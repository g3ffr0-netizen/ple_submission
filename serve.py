import json
from typing import Any, Dict


async def handle_ws_message(raw_message: str, engine: Any, run_harness_fn: Any) -> str:
    try:
        payload: Dict[str, Any] = json.loads(raw_message)
    except json.JSONDecodeError:
        return json.dumps({
            "type": "error",
            "code": "invalid_json",
            "message": "Message must be valid JSON.",
        })

    action = payload.get("action", "execute")

    if action == "reset":
        state = engine.reset()
        return json.dumps({
            "type": "reset",
            "status": "ok",
            "locked": state["locked"],
            "failure_count": state["failure_count"],
            "message": "Harness state reset.",
        })

    if getattr(engine, "locked", False):
        return json.dumps({
            "type": "lockout",
            "status": "locked",
            "locked": True,
            "failure_count": getattr(engine, "failure_count", 0),
            "requires_reset": True,
            "message": "Execution locked due to consecutive failures. Send action='reset' to unlock.",
        })

    result = await run_harness_fn(payload) if callable(run_harness_fn) else run_harness_fn

    if result.get("locked") or result.get("status") == "locked":
        return json.dumps({
            "type": "lockout",
            "status": "locked",
            "locked": True,
            "failure_count": result.get("failure_count", getattr(engine, "failure_count", 0)),
            "requires_reset": True,
            "error": result.get("error", "Harness locked."),
            "last_reason": result.get("last_reason", ""),
        })

    if not result.get("ok", False):
        return json.dumps({
            "type": "verification_failed",
            "status": result.get("status", "verification_retries_exhausted"),
            "locked": bool(result.get("locked", False)),
            "failure_count": result.get("failure_count", getattr(engine, "failure_count", 0)),
            "error": result.get("error", "Verification retries exhausted."),
            "last_reason": result.get("last_reason", ""),
        })

    return json.dumps({
        "type": "result",
        "status": "ok",
        "locked": bool(result.get("locked", False)),
        "failure_count": result.get("failure_count", getattr(engine, "failure_count", 0)),
        "attempt": result.get("attempt", 1),
        "model": result.get("model", getattr(engine, "model", "")),
        "output": result.get("output", ""),
    })
