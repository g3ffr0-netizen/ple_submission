"""WebSocket protocol handler and runnable server CLI for the Sovereign Harness."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import signal
import weakref
from typing import Any, Callable, Dict, List, Optional

from harness import HarnessExecutionEngine, make_verify_fn

logger = logging.getLogger(__name__)

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    websockets = None  # type: ignore[assignment]

    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        pass


try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

_LOOP_ENGINE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[int, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _get_engine_lock(engine: Any) -> asyncio.Lock:
    """Return a per-event-loop asyncio.Lock keyed by the engine instance."""
    loop = asyncio.get_running_loop()
    locks = _LOOP_ENGINE_LOCKS.setdefault(loop, {})
    key = id(engine)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _dumps(data: Dict[str, Any], request_id: Any = None) -> str:
    if request_id is not None:
        data["request_id"] = request_id
    return json.dumps(data)


async def handle_ws_message(raw_message: str, engine: Any, run_harness_fn: Any) -> str:
    if not isinstance(raw_message, str):
        return _dumps(
            {
                "type": "error",
                "code": "invalid_json",
                "message": "Message must be a valid JSON string.",
            }
        )

    try:
        payload = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Rejected malformed JSON WebSocket frame.")
        return _dumps(
            {
                "type": "error",
                "code": "invalid_json",
                "message": "Message must be valid JSON.",
            }
        )

    if not isinstance(payload, dict):
        return _dumps(
            {
                "type": "error",
                "code": "invalid_payload",
                "message": "JSON payload must be an object.",
            }
        )

    request_id = payload.get("request_id")
    raw_action = (
        payload.get("action")
        if payload.get("action") is not None
        else payload.get("type", "execute")
    )
    action = str(raw_action).strip().lower()

    if action == "ping":
        return _dumps(
            {
                "type": "pong",
                "status": "ok",
                "locked": bool(getattr(engine, "locked", False)),
                "failure_count": int(getattr(engine, "failure_count", 0)),
            },
            request_id,
        )

    if action == "status":
        return _dumps(
            {
                "type": "status",
                "status": "locked" if getattr(engine, "locked", False) else "ready",
                "locked": bool(getattr(engine, "locked", False)),
                "failure_count": int(getattr(engine, "failure_count", 0)),
                "model": getattr(engine, "model", ""),
            },
            request_id,
        )

    if action == "reset":
        async with _get_engine_lock(engine):
            state = (
                engine.reset()
                if hasattr(engine, "reset")
                else {"locked": False, "failure_count": 0}
            )
            return _dumps(
                {
                    "type": "reset",
                    "status": "ok",
                    "locked": bool(state.get("locked", False)),
                    "failure_count": int(state.get("failure_count", 0)),
                    "message": "Harness state reset.",
                },
                request_id,
            )

    if action != "execute":
        return _dumps(
            {
                "type": "error",
                "code": "unknown_action",
                "message": (
                    f"Unsupported action '{action}'. "
                    "Expected 'execute', 'reset', 'status', or 'ping'."
                ),
            },
            request_id,
        )

    async with _get_engine_lock(engine):
        if getattr(engine, "locked", False):
            return _dumps(
                {
                    "type": "lockout",
                    "status": "locked",
                    "locked": True,
                    "failure_count": int(getattr(engine, "failure_count", 0)),
                    "requires_reset": True,
                    "message": (
                        "Execution locked due to consecutive failures. "
                        "Send action='reset' to unlock."
                    ),
                },
                request_id,
            )

        try:
            if callable(run_harness_fn):
                maybe_result = run_harness_fn(payload)
                result = (
                    await maybe_result
                    if inspect.isawaitable(maybe_result)
                    else maybe_result
                )
            else:
                result = run_harness_fn
        except Exception as exc:
            return _dumps(
                {
                    "type": "error",
                    "code": "execution_error",
                    "locked": bool(getattr(engine, "locked", False)),
                    "failure_count": int(getattr(engine, "failure_count", 0)),
                    "message": f"Harness execution failed ({type(exc).__name__}): {exc}",
                },
                request_id,
            )

        if not isinstance(result, dict):
            return _dumps(
                {
                    "type": "error",
                    "code": "invalid_result",
                    "locked": bool(getattr(engine, "locked", False)),
                    "failure_count": int(getattr(engine, "failure_count", 0)),
                    "message": "Harness runner returned an invalid non-dict result.",
                },
                request_id,
            )

        if result.get("status") == "invalid_prompt":
            return _dumps(
                {
                    "type": "error",
                    "code": "invalid_prompt",
                    "status": "invalid_prompt",
                    "locked": bool(result.get("locked", getattr(engine, "locked", False))),
                    "failure_count": int(
                        result.get("failure_count", getattr(engine, "failure_count", 0))
                    ),
                    "message": result.get("error", "Prompt must be a non-empty string."),
                },
                request_id,
            )

        if result.get("locked") or result.get("status") == "locked":
            return _dumps(
                {
                    "type": "lockout",
                    "status": "locked",
                    "locked": True,
                    "failure_count": int(
                        result.get("failure_count", getattr(engine, "failure_count", 0))
                    ),
                    "requires_reset": True,
                    "error": result.get("error", "Harness locked."),
                    "last_reason": result.get("last_reason", result.get("error", "")),
                },
                request_id,
            )

        return _dumps(
            {
                "type": "result",
                "status": result.get("status", "ok"),
                "ok": bool(result.get("ok", True)),
                "output": result.get("output", ""),
                "model": result.get("model", getattr(engine, "model", "")),
                "attempt": result.get("attempt"),
                "reason": result.get("reason", ""),
                "history": result.get("history", []),
            },
            request_id,
        )


def _make_live_generate_fn(api_key: str) -> Callable[[str, str, int], str]:
    """Build a Gemini generation callback using google-genai."""
    if genai is None:
        raise RuntimeError(
            "google-genai is not installed. Run `python -m pip install -r requirements.txt`."
        )
    client = genai.Client(api_key=api_key, vertexai=False)

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
        return (getattr(response, "text", "") or "").strip()

    return _generate


def _make_dry_run_generate_fn(payload: Dict[str, Any]) -> Callable[[str, str, int], str]:
    """Build an offline deterministic generator for --dry-run testing."""
    def _generate(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        if "mock_output" in payload:
            return str(payload["mock_output"])
        phrase = (
            payload.get("required_phrase", "").strip()
            if isinstance(payload.get("required_phrase"), str)
            else ""
        )
        note = f"Dry-run verified response. {phrase}".strip()
        if bool(payload.get("require_json", False)):
            return json.dumps({"status": "ok", "note": note})
        return note

    return _generate


def build_run_harness_fn(
    engine: HarnessExecutionEngine,
    dry_run: bool = False,
    api_key: Optional[str] = None,
) -> Callable[[Dict[str, Any]], Any]:
    """Build the async execute callback passed to handle_ws_message."""
    live_gen: Optional[Callable[[str, str, int], str]] = None
    if not dry_run:
        resolved_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not resolved_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Export GEMINI_API_KEY or pass --dry-run."
            )
        live_gen = _make_live_generate_fn(resolved_key)

    async def _run_harness(payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = payload.get("prompt", "")
        min_chars = payload.get("min_chars", 1)
        required_phrase = payload.get("required_phrase", "")
        require_json = bool(payload.get("require_json", False))

        verify_fn = make_verify_fn(
            min_chars=min_chars,
            required_phrase=required_phrase,
            require_json=require_json,
        )
        gen_fn = _make_dry_run_generate_fn(payload) if dry_run else live_gen
        assert gen_fn is not None
        return await asyncio.to_thread(
            engine.run_with_verification,
            prompt,
            gen_fn,
            verify_fn,
        )

    return _run_harness


async def _handle_connection(
    websocket: Any,
    engine: Any,
    run_harness_fn: Any,
) -> None:
    """Serve frames on a single WebSocket connection until client disconnect."""
    try:
        async for raw_message in websocket:
            response = await handle_ws_message(raw_message, engine, run_harness_fn)
            await websocket.send(response)
    except ConnectionClosed:
        logger.debug("WebSocket client disconnected cleanly.")
        return


async def start_ws_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    config_path: str = "submission.yaml",
    dry_run: bool = False,
    engine: Optional[HarnessExecutionEngine] = None,
    run_harness_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Start and return the asyncio WebSocket server instance."""
    if websockets is None:
        raise RuntimeError(
            "websockets is not installed. Run `python -m pip install -r requirements.txt`."
        )

    active_engine = (
        engine if engine is not None else HarnessExecutionEngine(config_path=config_path)
    )
    active_runner = (
        run_harness_fn
        if run_harness_fn is not None
        else build_run_harness_fn(active_engine, dry_run=dry_run, api_key=api_key)
    )

    async def _handler(websocket: Any) -> None:
        await _handle_connection(websocket, active_engine, active_runner)

    return await websockets.serve(_handler, host, port)


async def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    config_path: str = "submission.yaml",
    dry_run: bool = False,
    stop_event: Optional[asyncio.Event] = None,
    api_key: Optional[str] = None,
    engine: Optional[HarnessExecutionEngine] = None,
) -> None:
    """Run the WebSocket server until stop_event is set or a termination signal arrives."""
    active_engine = (
        engine if engine is not None else HarnessExecutionEngine(config_path=config_path)
    )
    server = await start_ws_server(
        host=host,
        port=port,
        config_path=config_path,
        dry_run=dry_run,
        engine=active_engine,
        api_key=api_key,
    )

    bound_port = port
    if getattr(server, "sockets", None):
        sock = server.sockets[0]
        if sock is not None:
            bound_port = sock.getsockname()[1]

    mode = "dry-run" if dry_run else "live"
    logger.info(
        "WebSocket server listening on ws://%s:%d (mode=%s, model=%s)",
        host,
        bound_port,
        mode,
        active_engine.model,
    )
    print(
        f"Sovereign Harness WS server listening on ws://{host}:{bound_port} "
        f"(mode={mode}, model={active_engine.model})",
        flush=True,
    )

    active_stop = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, active_stop.set)
        except (NotImplementedError, ValueError, RuntimeError):
            pass

    try:
        await active_stop.wait()
    finally:
        logger.info("Shutting down WebSocket server on ws://%s:%d", host, bound_port)
        server.close()
        await server.wait_closed()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the WebSocket server entrypoint."""
    parser = argparse.ArgumentParser(
        description="Run the Sovereign Harness WebSocket server."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    parser.add_argument(
        "--config",
        default="submission.yaml",
        help="Path to submission.yaml (default: submission.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with an offline deterministic mock runner (no GEMINI_API_KEY required).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entrypoint for launching the WebSocket server."""
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(
            run_server(
                host=args.host,
                port=args.port,
                config_path=args.config,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
