"""
End-to-end live loopback WebSocket integration tests for serve.py.

Spins up a real websockets server on 127.0.0.1 with an ephemeral port (port=0)
in --dry-run mode and connects a real websockets client over the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HarnessExecutionEngine
from serve import parse_args, run_server, start_ws_server

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]


@unittest.skipIf(websockets is None, "websockets package is not installed")
class TestLiveWebSocketServer(unittest.TestCase):
    def test_live_ws_ping_status_malformed_execute_and_reset(self):
        async def _scenario():
            server = await start_ws_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                dry_run=True,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                uri = f"ws://127.0.0.1:{port}"
                async with websockets.connect(uri) as ws:
                    # 1. ping
                    await ws.send(json.dumps({"action": "ping", "request_id": "req-ping"}))
                    pong = json.loads(await ws.recv())
                    self.assertEqual(pong["type"], "pong")
                    self.assertEqual(pong["status"], "ok")
                    self.assertEqual(pong["request_id"], "req-ping")

                    # 2. status
                    await ws.send(json.dumps({"action": "status", "request_id": "req-status"}))
                    status = json.loads(await ws.recv())
                    self.assertEqual(status["type"], "status")
                    self.assertFalse(status["locked"])
                    self.assertEqual(status["failure_count"], 0)
                    self.assertEqual(status["model"], "gemini-4-hydrogen")

                    # 3. malformed JSON frame must return error without killing connection
                    await ws.send("{not valid json")
                    err = json.loads(await ws.recv())
                    self.assertEqual(err["type"], "error")
                    self.assertEqual(err["code"], "invalid_json")

                    # 4. execute (dry-run JSON + required phrase) over same connection
                    await ws.send(
                        json.dumps(
                            {
                                "action": "execute",
                                "request_id": "req-exec",
                                "prompt": "Confirm microgrid status",
                                "required_phrase": "microgrid verified",
                                "require_json": True,
                            }
                        )
                    )
                    res = json.loads(await ws.recv())
                    self.assertEqual(res["type"], "result")
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["status"], "verified")
                    self.assertEqual(res["request_id"], "req-exec")
                    parsed_out = json.loads(res["output"])
                    self.assertIn("microgrid verified", parsed_out["note"].lower())

                    # 5. reset
                    await ws.send(json.dumps({"action": "reset", "request_id": "req-reset"}))
                    reset = json.loads(await ws.recv())
                    self.assertEqual(reset["type"], "reset")
                    self.assertEqual(reset["status"], "ok")
                    self.assertFalse(reset["locked"])
                    self.assertEqual(reset["failure_count"], 0)
                    self.assertEqual(reset["request_id"], "req-reset")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_live_ws_lockout_and_reset_recovery(self):
        async def _scenario():
            engine = HarnessExecutionEngine(
                config_path=str(REPO_ROOT / "submission.yaml"),
                max_retries=1,
                max_failures=2,
            )
            server = await start_ws_server(
                host="127.0.0.1",
                port=0,
                dry_run=True,
                engine=engine,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    fail_payload = json.dumps(
                        {
                            "action": "execute",
                            "prompt": "Return JSON",
                            "require_json": True,
                            "mock_output": "not json",
                        }
                    )
                    # Failure 1 -> retries exhausted (not locked yet)
                    await ws.send(fail_payload)
                    r1 = json.loads(await ws.recv())
                    self.assertEqual(r1["type"], "result")
                    self.assertFalse(r1["ok"])
                    self.assertFalse(engine.locked)

                    # Failure 2 -> locks circuit breaker
                    await ws.send(fail_payload)
                    r2 = json.loads(await ws.recv())
                    self.assertEqual(r2["type"], "lockout")
                    self.assertTrue(r2["locked"])
                    self.assertEqual(r2["failure_count"], 2)

                    # Failure 3 -> blocked immediately while locked
                    await ws.send(
                        json.dumps({"action": "execute", "prompt": "Should be blocked"})
                    )
                    r3 = json.loads(await ws.recv())
                    self.assertEqual(r3["type"], "lockout")
                    self.assertTrue(r3["locked"])

                    # Reset unlocks engine; subsequent execute succeeds
                    await ws.send(json.dumps({"action": "reset"}))
                    rst = json.loads(await ws.recv())
                    self.assertEqual(rst["type"], "reset")
                    self.assertFalse(rst["locked"])

                    await ws.send(
                        json.dumps({"action": "execute", "prompt": "Recovered run"})
                    )
                    r4 = json.loads(await ws.recv())
                    self.assertEqual(r4["type"], "result")
                    self.assertTrue(r4["ok"])
                    self.assertEqual(r4["status"], "verified")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_live_ws_client_disconnect_and_graceful_shutdown(self):
        async def _scenario():
            # Part A: abrupt/clean client disconnects leave server healthy for next client
            server = await start_ws_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                dry_run=True,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                uri = f"ws://127.0.0.1:{port}"
                async with websockets.connect(uri) as ws1:
                    await ws1.send(json.dumps({"action": "ping"}))
                    self.assertEqual(json.loads(await ws1.recv())["type"], "pong")

                async with websockets.connect(uri) as ws2:
                    await ws2.send(json.dumps({"action": "status"}))
                    self.assertEqual(json.loads(await ws2.recv())["type"], "status")
            finally:
                server.close()
                await server.wait_closed()

            # Part B: run_server shuts down cleanly when stop_event is triggered
            stop_event = asyncio.Event()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                task = asyncio.create_task(
                    run_server(
                        host="127.0.0.1",
                        port=0,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                        dry_run=True,
                        stop_event=stop_event,
                    )
                )
                await asyncio.sleep(0.05)
                stop_event.set()
                await asyncio.wait_for(task, timeout=5.0)
            self.assertIn("mode=dry-run", buf.getvalue())

        asyncio.run(_scenario())


class TestServeCLIArgs(unittest.TestCase):
    def test_cli_parse_args_defaults_and_overrides(self):
        defaults = parse_args([])
        self.assertEqual(defaults.host, "127.0.0.1")
        self.assertEqual(defaults.port, 8765)
        self.assertEqual(defaults.config, "submission.yaml")
        self.assertFalse(defaults.dry_run)

        custom = parse_args(
            ["--host", "0.0.0.0", "--port", "9001", "--config", "custom.yaml", "--dry-run"]
        )
        self.assertEqual(custom.host, "0.0.0.0")
        self.assertEqual(custom.port, 9001)
        self.assertEqual(custom.config, "custom.yaml")
        self.assertTrue(custom.dry_run)


if __name__ == "__main__":
    unittest.main(verbosity=2)
