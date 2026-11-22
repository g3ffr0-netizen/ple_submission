"""Batch evaluation runner for the Sovereign Harness across dev task suites."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure repository root is importable when executed as `python eval/run.py`
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HarnessExecutionEngine, _safe_int, extract_json_payload

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]


def load_dev_tasks(tasks_path: Path) -> List[Dict[str, Any]]:
    """Load and validate task objects from a JSON or JSONL file."""
    if not tasks_path.exists():
        raise FileNotFoundError(f"Dev tasks file not found: {tasks_path}")
    if tasks_path.suffix.lower() == ".jsonl":
        tasks: List[Dict[str, Any]] = []
        with tasks_path.open("r", encoding="utf-8") as fh:
            for line_num, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Task entry on line {line_num} must be a JSON object."
                    )
                tasks.append(item)
        return tasks

    with tasks_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        raw_tasks = data["tasks"]
    elif isinstance(data, list):
        raw_tasks = data
    else:
        raise ValueError(
            "Unsupported dev tasks structure; expected a list or {'tasks': [...]}."
        )

    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise ValueError(f"Task entry at index {idx} must be a JSON object.")
    return raw_tasks


def build_task_verifier(task: Dict[str, Any]) -> Callable[[Any], Tuple[bool, str]]:
    """Build a per-task verifier from optional task constraints."""
    raw_terms = task.get("required_terms")
    required_terms = raw_terms if isinstance(raw_terms, list) else []
    min_chars = _safe_int(task.get("min_chars"), default=1, minimum=1)
    require_json = bool(task.get("require_json", False))

    def _verify(output: Any) -> Tuple[bool, str]:
        if output is None:
            return False, "Empty output."
        if not isinstance(output, str):
            return False, f"Output must be a string (got {type(output).__name__})."
        text = output.strip()
        if not text:
            return False, "Empty output."
        if len(text) < min_chars:
            return False, f"Output too short ({len(text)} < {min_chars} chars)."
        lowered = text.lower()
        missing = [t for t in required_terms if str(t).lower() not in lowered]
        if missing:
            return False, f"Missing required terms: {', '.join(map(str, missing))}."
        if require_json:
            cleaned = extract_json_payload(text)
            try:
                json.loads(cleaned)
            except json.JSONDecodeError as exc:
                return False, f"Invalid JSON output ({exc.msg})."
            except RecursionError:
                return False, "Output JSON exceeds maximum nesting depth."
        return True, "Verified."

    return _verify


def make_live_generate_fn(api_key: str) -> Callable[[str, str, int], str]:
    """Build a live Gemini generation callback backed by a shared genai.Client."""
    if genai is None:
        raise RuntimeError(
            "google-genai is not installed. Run `pip install google-genai`."
        )
    base_url = os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip()
    client = (
        genai.Client(
            api_key=api_key,
            vertexai=False,
            http_options={"base_url": base_url},
        )
        if base_url
        else genai.Client(api_key=api_key, vertexai=False)
    )

    def _generate(prompt: str, model: str, max_output_tokens: int) -> str:
        config = (
            types.GenerateContentConfig(max_output_tokens=max_output_tokens)
            if types is not None and max_output_tokens
            else None
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (getattr(response, "text", None) or "").strip()

    return _generate


def make_dry_run_generate_fn(task: Dict[str, Any]) -> Callable[[str, str, int], str]:
    """Build a deterministic offline generator for --dry-run evaluations."""
    min_chars = _safe_int(task.get("min_chars"), default=1, minimum=1)
    terms = " ".join(str(t) for t in (task.get("required_terms") or []))
    require_json = bool(task.get("require_json", False))

    def _generate(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        if "mock_output" in task:
            return str(task["mock_output"])
        if require_json:
            payload: Dict[str, Any] = {"status": "ok", "terms": terms}
            rendered = json.dumps(payload)
            if len(rendered) < min_chars:
                payload["padding"] = "x" * (min_chars - len(rendered) + 16)
                rendered = json.dumps(payload)
            return rendered
        text = f"Dry-run verified response. {terms}".strip()
        if len(text) < min_chars:
            text = f"{text} {'.' * (min_chars - len(text))}"
        return text

    return _generate


def evaluate_tasks(
    tasks: List[Dict[str, Any]],
    run_task_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    reset_fn: Optional[Callable[[], Any]] = None,
    reset_on_lockout: bool = True,
) -> Dict[str, Any]:
    """Evaluate tasks sequentially and aggregate accuracy, latency, and model metrics."""
    results: List[Dict[str, Any]] = []
    model_usage: Dict[str, Dict[str, int]] = {}
    passed_count = 0
    lockout_count = 0
    total_elapsed_ms = 0.0

    for idx, task in enumerate(tasks, start=1):
        task_id = task.get("id", f"task_{idx:02d}")
        outcome = run_task_fn(task)
        ok = bool(outcome.get("ok", False))
        locked = bool(outcome.get("locked", False))
        try:
            elapsed_ms = float(outcome.get("elapsed_ms", 0.0) or 0.0)
        except (TypeError, ValueError):
            elapsed_ms = 0.0
        total_elapsed_ms += elapsed_ms

        if ok:
            passed_count += 1
        if locked:
            lockout_count += 1

        history = outcome.get("history")
        if isinstance(history, list) and history:
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                model_name = str(entry.get("model") or "").strip()
                if not model_name:
                    continue
                stats = model_usage.setdefault(
                    model_name, {"attempts": 0, "verified": 0, "failed_attempts": 0}
                )
                stats["attempts"] += 1
                if entry.get("ok"):
                    stats["verified"] += 1
                else:
                    stats["failed_attempts"] += 1
        else:
            fallback_model = str(outcome.get("model") or "").strip()
            if fallback_model:
                stats = model_usage.setdefault(
                    fallback_model, {"attempts": 0, "verified": 0, "failed_attempts": 0}
                )
                stats["attempts"] += 1
                if ok:
                    stats["verified"] += 1
                else:
                    stats["failed_attempts"] += 1

        results.append(
            {
                "task_id": task_id,
                "ok": ok,
                "status": outcome.get("status", "unknown"),
                "model": outcome.get("model", ""),
                "attempts": outcome.get(
                    "attempt", len(history) if isinstance(history, list) else 0
                ),
                "elapsed_ms": round(elapsed_ms, 2),
                "locked": locked,
                "error": outcome.get("error"),
                "last_reason": outcome.get("last_reason"),
                "history": history if isinstance(history, list) else [],
            }
        )

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
        "model_usage": model_usage,
        "results": results,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the evaluation runner."""
    parser = argparse.ArgumentParser(
        description="Run PLE development task evaluation."
    )
    parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("eval/dev_tasks.json"),
        help="Path to dev tasks JSON/JSONL.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="submission.yaml",
        help="Path to submission.yaml.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval/results.json"),
        help="Output report path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run offline with mock outputs (no API calls).",
    )
    parser.add_argument(
        "--no-reset-on-lockout",
        action="store_true",
        help="Do not reset harness after a lockout.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entrypoint for evaluating tasks against HarnessExecutionEngine."""
    args = parse_args(argv)
    try:
        tasks = load_dev_tasks(args.tasks)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc

    engine = HarnessExecutionEngine(config_path=args.config)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        raise SystemExit(
            "GEMINI_API_KEY is not set. Export GEMINI_API_KEY or pass --dry-run for offline evaluation."
        )

    live_gen = make_live_generate_fn(api_key) if not args.dry_run else None

    def run_single_task(task: Dict[str, Any]) -> Dict[str, Any]:
        raw_prompt = task.get("prompt", "")
        prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
        gen_fn = make_dry_run_generate_fn(task) if args.dry_run else live_gen
        assert gen_fn is not None
        verify_fn = build_task_verifier(task)
        return engine.run_with_verification(
            prompt=prompt, generate_fn=gen_fn, verify_fn=verify_fn
        )

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

    usage_summary = ", ".join(
        f"{m}:{s['verified']}/{s['attempts']}"
        for m, s in sorted(report["model_usage"].items())
    ) or "none"
    print(
        f"Evaluated {report['total_tasks']} tasks ({report['mode']}): "
        f"{report['passed']}/{report['total_tasks']} passed "
        f"({report['pass_rate'] * 100:.1f}%), "
        f"avg={report['avg_elapsed_ms']:.2f} ms, "
        f"lockouts={report['lockouts']}, "
        f"models=[{usage_summary}]. Report saved to {args.out}"
    )


if __name__ == "__main__":
    main()
