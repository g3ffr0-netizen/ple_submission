from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure repository root is importable when executed as `python eval/run.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HarnessExecutionEngine

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]


def load_dev_tasks(tasks_path: Path) -> List[Dict[str, Any]]:
    if not tasks_path.exists():
        raise FileNotFoundError(f"Dev tasks file not found: {tasks_path}")
    if tasks_path.suffix.lower() == ".jsonl":
        tasks = []
        with tasks_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    tasks.append(json.loads(line))
        return tasks
    with tasks_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "tasks" in data:
        return data["tasks"]
    if isinstance(data, list):
        return data
    raise ValueError("Unsupported dev tasks structure; expected a list or {'tasks': [...]}.")


def build_task_verifier(task: Dict[str, Any]) -> Callable[[str], Tuple[bool, str]]:
    """Build a per-task verifier from optional task constraints (required_terms, min_chars, require_json)."""
    required_terms = task.get("required_terms") or []
    min_chars = int(task.get("min_chars", 1))
    require_json = bool(task.get("require_json", False))

    def _verify(output: str) -> Tuple[bool, str]:
        text = (output or "").strip()
        if not text:
            return False, "Empty output."
        if len(text) < min_chars:
            return False, f"Output too short ({len(text)} < {min_chars} chars)."
        lowered = text.lower()
        missing = [t for t in required_terms if str(t).lower() not in lowered]
        if missing:
            return False, f"Missing required terms: {', '.join(map(str, missing))}."
        if require_json:
            cleaned = text
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(lines[1:-1] if len(lines) >= 3 and lines[-1].strip() == "```" else lines[1:])
            try:
                json.loads(cleaned)
            except json.JSONDecodeError as exc:
                return False, f"Invalid JSON output ({exc.msg})."
        return True, "Verified."

    return _verify


def make_live_generate_fn(api_key: str) -> Callable[[str, str, int], str]:
    def _generate(prompt: str, model: str, max_output_tokens: int) -> str:
        if genai is None:
            raise RuntimeError("google-genai is not installed. Run `pip install google-genai`.")
        client = genai.Client(api_key=api_key, vertexai=False)
        config = types.GenerateContentConfig(max_output_tokens=max_output_tokens) if types else None
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (getattr(response, "text", "") or "").strip()

    return _generate


def make_dry_run_generate_fn(task: Dict[str, Any]) -> Callable[[str, str, int], str]:
    def _generate(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        if "mock_output" in task:
            return str(task["mock_output"])
        terms = " ".join(str(t) for t in (task.get("required_terms") or []))
        if task.get("require_json"):
            return json.dumps({"status": "ok", "terms": terms})
        return f"Dry-run verified response. {terms}".strip()

    return _generate


def evaluate_tasks(
    tasks: List[Dict[str, Any]],
    run_task_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    reset_fn: Optional[Callable[[], Any]] = None,
    reset_on_lockout: bool = True,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    passed_count = 0
    lockout_count = 0
    total_elapsed_ms = 0.0

    for idx, task in enumerate(tasks, start=1):
        task_id = task.get("id", f"task_{idx:02d}")
        outcome = run_task_fn(task)
        ok = bool(outcome.get("ok", False))
        locked = bool(outcome.get("locked", False))
        elapsed_ms = float(outcome.get("elapsed_ms", 0.0))
        total_elapsed_ms += elapsed_ms

        if ok:
            passed_count += 1
        if locked:
            lockout_count += 1

        results.append({
            "task_id": task_id,
            "ok": ok,
            "status": outcome.get("status", "unknown"),
            "model": outcome.get("model", ""),
            "attempts": outcome.get("attempt", len(outcome.get("history", []))),
            "elapsed_ms": round(elapsed_ms, 2),
            "locked": locked,
            "error": outcome.get("error"),
            "last_reason": outcome.get("last_reason"),
            "history": outcome.get("history", []),
        })

        if locked and reset_on_lockout and reset_fn is not None:
            reset_fn()

    total = len(results)
    return {
        "total_tasks": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "lockouts": lockout_count,
        "pass_rate": round((passed_count / total), 4) if total else 0.0,
        "total_elapsed_ms": round(total_elapsed_ms, 2),
        "avg_elapsed_ms": round((total_elapsed_ms / total), 2) if total else 0.0,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PLE development task evaluation.")
    parser.add_argument("--tasks", type=Path, default=Path("eval/dev_tasks.json"), help="Path to dev tasks JSON/JSONL.")
    parser.add_argument("--config", type=str, default="submission.yaml", help="Path to submission.yaml.")
    parser.add_argument("--out", type=Path, default=Path("eval/results.json"), help="Output report path.")
    parser.add_argument("--dry-run", action="store_true", help="Run offline with mock outputs (no API calls).")
    parser.add_argument("--no-reset-on-lockout", action="store_true", help="Do not reset harness after a lockout.")
    args = parser.parse_args()

    tasks = load_dev_tasks(args.tasks)
    engine = HarnessExecutionEngine(config_path=args.config)

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        raise SystemExit("GEMINI_API_KEY is not set. Export GEMINI_API_KEY or pass --dry-run for offline evaluation.")

    live_gen = make_live_generate_fn(api_key) if not args.dry_run else None

    def run_single_task(task: Dict[str, Any]) -> Dict[str, Any]:
        prompt = str(task.get("prompt", "")).strip()
        gen_fn = make_dry_run_generate_fn(task) if args.dry_run else live_gen
        verify_fn = build_task_verifier(task)
        return engine.run_with_verification(prompt=prompt, generate_fn=gen_fn, verify_fn=verify_fn)

    report = evaluate_tasks(
        tasks=tasks,
        run_task_fn=run_single_task,
        reset_fn=engine.reset,
        reset_on_lockout=not args.no_reset_on_lockout,
    )
    report["mode"] = "dry_run" if args.dry_run else "live"
    report["primary_model"] = engine.model
    report["fallback_models"] = engine.fallback_models

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"Evaluated {report['total_tasks']} tasks ({report['mode']}): "
        f"{report['passed']}/{report['total_tasks']} passed ({report['pass_rate'] * 100:.1f
