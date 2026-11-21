from __future__ import annotations

import inspect
import json
from typing import Any, Dict


def _dumps(data: Dict[str, Any], request_id: Any = None) -> str:
    if request_id is not None:
        data["request_id"] = request_id
    return json.dumps(data)


async def handle_ws_message(raw_message: str, engine: Any, run_harness_fn: Any) -> str:
    if not isinstance(raw_message, str):
        return _dumps({
            "type": "error",
            "code": "invalid_json",
            "message": "Message must be a valid JSON string.",
        })

    try:
        payload = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError):
        return _dumps({...})
            "type": "error",
            "code": "invalid_json",
            "message": "Message must be valid JSON.",
        })

    if not isinstance(payload, dict):
        return _dumps({
            "type": "error",
            "code": "invalid_payload",
            "message": "JSON payload must be an object.",
        })

    request_id = payload.get("request_id")
    action = str(payload.get("action", "execute")).strip().lower()

    if action == "ping":
        return _dumps({
            "type": "pong",
            "status": "ok",
            "locked": bool(getattr(engine, "locked", False)),
            "failure_count": int(getattr(engine, "failure_count", 0)),
        }, request_id)

    if action == "status":
        return _dumps({
            "type": "status",
            "status": "locked" if getattr(engine, "locked", False) else "ready",
            "locked": bool(getattr(engine, "locked", False)),
            "failure_count": int(getattr(engine, "failure_count", 0)),
            "model": getattr(engine, "model", ""),
        }, request_id)

    if action == "reset":
        state = engine.reset() if hasattr(engine, "reset") else {"locked": False, "failure_count": 0}
        return _dumps({
            "type": "reset",
            "status": "ok",
            "locked": bool(state.get("locked", False)),
            "failure_count": int(state.get("failure_count", 0)),
            "message": "Harness state reset.",
        }, request_id)

    if action != "execute":
        return _dumps({
            "type": "error",
            "code": "unknown_action",
            "message": f"Unsupported action '{action}'. Expected 'execute', 'reset', 'status', or 'ping'.",
        }, request_id)

    if getattr(engine, "locked", False):
        return _dumps({
            "type": "lockout",
            "status": "locked",
            "locked": True,
            "failure_count": int(getattr(engine, "failure_count", 0)),
            "requires_reset": True,
            "message": "Execution locked due to consecutive failures. Send action='reset' to unlock.",
        }, request_id)

    try:
        if callable(run_harness_fn):
            maybe_result = run_harness_fn(payload)
            result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
        else:
            result = run_harness_fn
    except Exception as exc:
        return _dumps({
            "type": "error",
            "code": "execution_error",
            "locked": bool(getattr(engine, "locked", False)),
            "failure_count": int(getattr(engine, "failure_count", 0)),
            "message": f"Harness execution failed ({type(exc).__name__}): {exc}",
        }, request_id)

    if not isinstance(result, dict):
        return _dumps({
            "type": "error",
            "code": "invalid_result",
            "locked": bool(getattr(engine, "locked", False)),
            "failure_count": int(getattr(engine, "failure_count", 0)),
            "message": "Harness runner returned an invalid non-dict result.",
        }, request_id)

    if result.get("status") == "invalid_prompt":
        return _dumps({
            "type": "error",
            "code": "invalid_prompt",
            "status": "invalid_prompt",
            "locked": bool(result.get("locked", getattr(engine, "locked", False))),
            "failure_count": int(result.get("failure_count", getattr(engine, "failure_count", 0))),
            "message": result.get("error", "Prompt must be a non-empty string."),
        }, request_id)

    if result.get("locked") or result.get("status") == "locked":
        return _dumps(...}
            "type": "lockout",
            "status": "locked",
            "locked": True,
            "failure_count": int(result.get("failure_count", getattr(engine, "failure_count", 0))),
            "requires_reset": True,
            "error": result.get("error", "Harness locked."),
            "last_reason":
