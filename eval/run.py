import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, List


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


def evaluate_tasks(
    tasks: List[Dict[str, Any]],
    run_task_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    reset_fn: Callable[[], None] | None = None,
    reset_on_lockout: bool = True,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    passed_count = 0

    for idx, task in enumerate(tasks, start=1):
        task_id = task.get("id", f"task_{idx:02d}")
        outcome = run_task_fn(task)
        ok = bool(outcome.get("ok", False))
        if ok:
            passed_count += 1

        results.append({
            "task_id": task_id,
            "ok": ok,
            "status": outcome.get("status", "unknown"),
            "attempts": outcome.get("attempt", 0),
            "error": outcome.get("error"),
        })

        if outcome.get("locked") and reset_on_lockout and reset_fn is not None:
            reset_fn()

    total = len(results)
    return {
        "total_tasks": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "pass_rate": (passed_count / total) if total else 0.0,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PLE development task evaluation.")
    parser.add_argument("--tasks", type=Path, default=Path("eval/dev_tasks.json"), help="Path to dev tasks JSON/JSONL.")
    parser.add_argument("--out", type=Path, default=Path("eval/results.json"), help="Output report path.")
    parser.add_argument("--no-reset-on-lockout", action="store_true", help="Do not reset harness after a lockout.")
    args = parser.parse_args()

    tasks = load_dev_tasks(args.tasks)
    print(f"Loaded {len(tasks)} tasks from {args.tasks}. Ready to wire generate_fn and verify_fn.")

    # Wire generate_fn and verify_fn to your Gemini client and PLE verifier before running live batches.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"loaded_tasks": len(tasks), "status": "ready"}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
