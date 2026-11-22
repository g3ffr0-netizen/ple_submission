"""End-to-end live loopback WebSocket and Protocol v1 integration tests for serve.py."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import websockets
import yaml

import serve
import taskbridge
from harness import CircuitBreaker, load_submission_config


class _MockEvaluatorSession:
    """In-memory PLE Protocol v1 evaluator side for loopback WebSocket tests."""

    def __init__(
        self,
        initial_files: Optional[Dict[str, bytes]] = None,
        mcp_tools: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.files: Dict[str, bytes] = dict(initial_files or {})
        self.mcp_tools = list(mcp_tools or [])
        self.logs: List[str] = []
        self.usages: List[Dict[str, Any]] = []
        self.requests: Dict[str, int] = {"exec": 0, "read_file": 0, "write_file": 0, "http": 0}
        self.mcp_calls: List[Dict[str, Any]] = []

    async def drive(
        self,
        ws: Any,
        instruction: str,
        *,
        workdir: str = "/app",
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        cancel_after_requests: Optional[int] = None,
    ) -> Dict[str, Any]:
        task_msg = {
            "type": "task",
            "protocol": 1,
            "session": "loopback-test",
            "instruction": instruction,
            "workdir": workdir,
            "limits": {"agent_timeout_sec": 300, "exec_timeout_sec": 120},
            "mcp_servers": mcp_servers or [],
        }
        await ws.send(json.dumps(task_msg))
        total_reqs = 0
        cancel_sent = False

        while True:
            raw = await ws.recv()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "log":
                self.logs.append(str(msg.get("text") or ""))
                continue
            if mtype == "usage":
                self.usages.append(msg)
                continue
            if mtype in ("done", "error"):
                return msg

            rid = msg["id"]
            if mtype in self.requests:
                self.requests[mtype] += 1
                total_reqs += 1

            if mtype == "exec":
                reply = {
                    "type": "result",
                    "id": rid,
                    "ok": True,
                    "exit_code": 0,
                    "output": f"{workdir}\n" + "\n".join(sorted(self.files)),
                    "truncated": False,
                    "timed_out": False,
                }
            elif mtype == "read_file":
                path = msg["path"]
                if path in self.files:
                    data = self.files[path]
                    reply = {
                        "type": "result",
                        "id": rid,
                        "ok": True,
                        "content_b64": base64.b64encode(data).decode(),
                        "size": len(data),
                        "truncated": False,
                    }
                else:
                    reply = {
                        "type": "result",
                        "id": rid,
                        "ok": False,
                        "error": f"No such file: {path}",
                    }
            elif mtype == "write_file":
                path = msg["path"]
                chunk = base64.b64decode(msg["content_b64"])
                existing = self.files.get(path, b"") if msg.get("append") else b""
                self.files[path] = existing + chunk
                reply = {
                    "type": "result",
                    "id": rid,
                    "ok": True,
                    "size": len(self.files[path]),
                }
            elif mtype == "http":
                body_bytes = base64.b64decode(msg.get("body_b64") or "")
                reply = self._handle_mcp_http(rid, body_bytes)
            else:
                reply = {"type": "result", "id": rid, "ok": False, "error": f"unknown type {mtype}"}

            await ws.send(json.dumps(reply))
            if (
                cancel_after_requests is not None
                and not cancel_sent
                and total_reqs >= cancel_after_requests
            ):
                cancel_sent = True
                await ws.send(json.dumps({"type": "cancel", "reason": "timeout"}))

    def _handle_mcp_http(self, rid: str, body_bytes: bytes) -> Dict[str, Any]:
        rpc = json.loads(body_bytes.decode("utf-8"))
        method = rpc.get("method")
        if method == "initialize":
            res_body = {
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "mock-mcp", "version": "1.0"},
                },
            }
            return {
                "type": "result",
                "id": rid,
                "ok": True,
                "status": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "mcp-sess-1",
                },
                "body_b64": base64.b64encode(json.dumps(res_body).encode()).decode(),
                "truncated": False,
            }
        if method == "notifications/initialized":
            return {
                "type": "result",
                "id": rid,
                "ok": True,
                "status": 202,
                "headers": {},
                "body_b64": "",
                "truncated": False,
            }
        if method == "tools/list":
            res_body = {
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {"tools": self.mcp_tools},
            }
            return {
                "type": "result",
                "id": rid,
                "ok": True,
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body_b64": base64.b64encode(json.dumps(res_body).encode()).decode(),
                "truncated": False,
            }
        if method == "tools/call":
            params = rpc.get("params") or {}
            self.mcp_calls.append(params)
            res_body = {
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {
                    "content": [{"type": "text", "text": "PLE-MCP-SECRET-42"}],
                    "isError": False,
                },
            }
            return {
                "type": "result",
                "id": rid,
                "ok": True,
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body_b64": base64.b64encode(json.dumps(res_body).encode()).decode(),
                "truncated": False,
            }
        return {
            "type": "result",
            "id": rid,
            "ok": False,
            "error": f"Unsupported MCP method {method}",
        }


class TestLivePLEProtocolV1(unittest.TestCase):
    def test_live_ple_protocol_v1_task_exec_read_write_done(self):
        async def _scenario() -> None:
            server = await serve.start_ple_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                dry_run=True,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                evaluator = _MockEvaluatorSession()
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    max_size=taskbridge.MAX_FRAME,
                    ping_interval=None,
                ) as ws:
                    final = await evaluator.drive(
                        ws, "Write HELLO to /app/hello.txt and summarize."
                    )

                self.assertEqual(final["type"], "done")
                self.assertTrue(final["final_message"].strip())
                self.assertEqual(evaluator.files.get("/app/hello.txt"), b"HELLO\n")
                self.assertGreaterEqual(evaluator.requests["exec"], 1)
                self.assertGreaterEqual(evaluator.requests["write_file"], 1)
                self.assertGreaterEqual(evaluator.requests["read_file"], 1)
                self.assertGreaterEqual(len(evaluator.logs), 1)
                self.assertGreaterEqual(len(evaluator.usages), 3)
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_live_ple_protocol_v1_mcp_tool_discovery_and_call(self):
        async def _scenario() -> None:
            server = await serve.start_ple_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                dry_run=True,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                evaluator = _MockEvaluatorSession(
                    mcp_tools=[
                        {
                            "name": "get_word",
                            "description": "Retrieve the secret word.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"tag": {"type": "string"}},
                                "required": ["tag"],
                            },
                        }
                    ]
                )
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    max_size=taskbridge.MAX_FRAME,
                    ping_interval=None,
                ) as ws:
                    final = await evaluator.drive(
                        ws,
                        "Call the MCP tool and write the result to /app/mcp.txt.",
                        mcp_servers=[{"name": "calc", "url": "http://127.0.0.1:9000/mcp"}],
                    )

                self.assertEqual(final["type"], "done")
                self.assertGreaterEqual(evaluator.requests["http"], 4)
                self.assertEqual(len(evaluator.mcp_calls), 1)
                self.assertEqual(evaluator.mcp_calls[0]["name"], "get_word")
                self.assertEqual(evaluator.files.get("/app/mcp.txt"), b"PLE-MCP-SECRET-42\n")
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_live_ple_protocol_v1_cancel_sends_done_gracefully(self):
        class _SlowMockClient:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.chat = self
                self.completions = self

            async def create(self, **_kwargs: Any) -> Any:
                self.started.set()
                await asyncio.sleep(30)
                raise AssertionError("Should have been cancelled before waking")

        async def _scenario() -> None:
            slow_client = _SlowMockClient()
            server = await serve.start_ple_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                client=slow_client,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}",
                    max_size=taskbridge.MAX_FRAME,
                    ping_interval=None,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "task",
                                "protocol": 1,
                                "session": "cancel-test",
                                "instruction": "Long task",
                                "workdir": "/app",
                                "limits": {"agent_timeout_sec": 300},
                            }
                        )
                    )
                    await asyncio.wait_for(slow_client.started.wait(), timeout=2.0)
                    await ws.send(json.dumps({"type": "cancel", "reason": "timeout"}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=6.0))
                    self.assertEqual(reply["type"], "done")
                    self.assertTrue(reply["final_message"])
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_live_ple_protocol_v1_malformed_and_non_task_first_frame_plus_shutdown(self):
        async def _scenario() -> None:
            stop_event = asyncio.Event()
            buf = io.StringIO()
            server_task = None
            with contextlib.redirect_stdout(buf):
                server = await serve.start_ple_server(
                    host="127.0.0.1",
                    port=0,
                    config_path=str(REPO_ROOT / "submission.yaml"),
                    dry_run=True,
                )
            try:
                port = server.sockets[0].getsockname()[1]
                uri = f"ws://127.0.0.1:{port}"

                # 1. Malformed JSON first frame -> error
                async with websockets.connect(uri) as ws1:
                    await ws1.send("{bad json")
                    err1 = json.loads(await ws1.recv())
                    self.assertEqual(err1["type"], "error")
                    self.assertIn("expected a task message first", err1["message"])

                # 2. Non-task JSON object first frame -> error
                async with websockets.connect(uri) as ws2:
                    await ws2.send(json.dumps({"type": "ping"}))
                    err2 = json.loads(await ws2.recv())
                    self.assertEqual(err2["type"], "error")

                # 3. Healthy subsequent task works normally
                evaluator = _MockEvaluatorSession()
                async with websockets.connect(uri) as ws3:
                    done = await evaluator.drive(ws3, "Follow-up task")
                    self.assertEqual(done["type"], "done")
            finally:
                server.close()
                await server.wait_closed()

            # 4. Graceful shutdown via run_server stop_event
            with contextlib.redirect_stdout(buf):
                server_task = asyncio.create_task(
                    serve.run_server(
                        host="127.0.0.1",
                        port=0,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                        dry_run=True,
                        stop_event=stop_event,
                    )
                )
                await asyncio.sleep(0.05)
                stop_event.set()
                await asyncio.wait_for(server_task, timeout=5.0)
            self.assertIn("listening on ", buf.getvalue())

        asyncio.run(_scenario())


class TestPLEAlignmentUnitContracts(unittest.TestCase):
    def test_runtime_config_supports_all_evaluator_env_vars(self):
        cfg_path = str(REPO_ROOT / "submission.yaml")

        # OpenAI proxy vars take priority
        with patch.dict(
            os.environ,
            {
                "MODEL_NAME": "gemini-4-argon",
                "OPENAI_BASE_URL": "http://proxy.local:8080/v1/",
                "OPENAI_API_KEY": "openai-proxy-token",
                "GOOGLE_GEMINI_BASE_URL": "http://ignored.local",
                "GEMINI_API_KEY": "ignored-key",
            },
            clear=False,
        ):
            rt1 = serve.resolve_runtime_config(cfg_path)
            self.assertEqual(rt1["model"], "gemini-4-argon")
            self.assertEqual(rt1["base_url"], "http://proxy.local:8080/v1")
            self.assertEqual(rt1["api_key"], "openai-proxy-token")
            self.assertEqual(rt1["effort"], "high")
            self.assertEqual(rt1["max_output_tokens"], 32000)

        # Fallback to MODEL_BASE_URL / MODEL_API_KEY (submission.yaml REASONING_EFFORT="high" honored)
        with patch.dict(
            os.environ,
            {
                "OPENAI_BASE_URL": "",
                "OPENAI_API_KEY": "",
                "REASONING_EFFORT": "",
                "MODEL_NAME": "claude-opus-5-5",
                "MODEL_BASE_URL": "http://model-proxy.local/v1",
                "MODEL_API_KEY": "model-token",
            },
            clear=False,
        ):
            rt2 = serve.resolve_runtime_config(cfg_path)
            self.assertEqual(rt2["model"], "claude-opus-5-5")
            self.assertEqual(rt2["base_url"], "http://model-proxy.local/v1")
            self.assertEqual(rt2["api_key"], "model-token")
            self.assertEqual(rt2["effort"], "high")
            self.assertTrue(rt2["cache_mark"])

            # Without yaml override, MAX_EFFORT_MODELS auto-selects "max"
            rt_auto = serve.resolve_runtime_config("nonexistent_submission.yaml")
            self.assertEqual(rt_auto["effort"], "max")

        # Explicit REASONING_EFFORT env overrides submission.yaml env
        with patch.dict(
            os.environ,
            {"REASONING_EFFORT": "medium"},
            clear=False,
        ):
            rt_override = serve.resolve_runtime_config(cfg_path)
            self.assertEqual(rt_override["effort"], "medium")

        # Fallback to GOOGLE_GEMINI_BASE_URL / GEMINI_API_KEY
        with patch.dict(
            os.environ,
            {
                "OPENAI_BASE_URL": "",
                "OPENAI_API_KEY": "",
                "MODEL_BASE_URL": "",
                "MODEL_API_KEY": "",
                "MODEL_NAME": "",
                "GOOGLE_GEMINI_BASE_URL": "https://generativelanguage.googleapis.com",
                "GEMINI_API_KEY": "gemini-token",
            },
            clear=False,
        ):
            rt3 = serve.resolve_runtime_config(cfg_path)
            self.assertEqual(rt3["model"], "gemini-3.8-flash")
            self.assertEqual(
                rt3["base_url"],
                "https://generativelanguage.googleapis.com/v1beta/openai",
            )
            self.assertEqual(rt3["api_key"], "gemini-token")

    def test_submission_yaml_matches_official_ple_schema(self):
        raw_text = (REPO_ROOT / "submission.yaml").read_bytes()
        self.assertNotIn(b"\r\n", raw_text, "submission.yaml must use LF endings")
        data = yaml.safe_load(raw_text.decode("utf-8"))
        self.assertIsInstance(data, dict)

        allowed_keys = {"name", "model_api", "model_base_url", "install", "serve", "env"}
        self.assertTrue(set(data.keys()).issubset(allowed_keys))
        self.assertRegex(str(data.get("name", "")), r"^[a-z0-9][a-z0-9-]{1,38}$")

        valid_gc_models = {
            "google-cloud/gemini-4-argon",
            "google-cloud/gemini-3.8-flash",
            "google-cloud/gemini-3.1-pro-preview",
            "google-cloud/claude-opus-5-5",
            "google-cloud/gpt-oss-120b",
            "google-cloud/glm-5.2",
            "google-cloud/gemma-4-26b",
            "google-cloud/llama-4-maverick",
        }
        self.assertIn(data.get("model_api"), valid_gc_models)
        self.assertTrue(str(data.get("serve", "")).strip())

        reserved_env = {
            "PORT", "HOME", "USER", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "NO_PROXY", "NODE_USE_ENV_PROXY", "MODEL_API", "MODEL_NAME",
            "MODEL_BASE_URL", "MODEL_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
            "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "GOOGLE_GEMINI_BASE_URL",
            "GEMINI_API_KEY",
        }
        env_map = data.get("env") or {}
        self.assertIsInstance(env_map, dict)
        for k, v in env_map.items():
            self.assertRegex(k, r"^[A-Z_][A-Z0-9_]{0,63}$")
            self.assertNotIn(k, reserved_env)
            self.assertIsInstance(v, str)
            self.assertNotIn("${", v)

    def test_edit_file_tool_and_circuit_breaker_recovery(self):
        class _FakeSession:
            workdir = "/app"

            def __init__(self) -> None:
                self.files: Dict[str, bytes] = {"/app/code.py": b"x = 1\nx = 1\ny = 2\n"}
                self.logs: List[str] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> Optional[float]:
                return 600.0

            async def read_file(self, path: str) -> bytes:
                if path not in self.files:
                    raise taskbridge.TaskError(f"missing: {path}")
                return self.files[path]

            async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
                raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                self.files[path] = raw
                return len(raw)

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, *_args: Any) -> None:
                return None

        async def _scenario() -> None:
            fake_session = _FakeSession()
            # Ambiguous match without replace_all -> error
            res1 = await serve.edit_file_in_session(fake_session, "code.py", "x = 1", "x = 9")
            self.assertTrue(res1.startswith("error:"))

            # replace_all=True succeeds
            res2 = await serve.edit_file_in_session(
                fake_session, "code.py", "x = 1", "x = 9", replace_all=True
            )
            self.assertIn("replaced 2 occurrence(s)", res2)
            self.assertEqual(fake_session.files["/app/code.py"], b"x = 9\nx = 9\ny = 2\n")

            # Agent tool CircuitBreaker injects guidance after max_failures=2 consecutive tool errors
            class _ErrorThenRecoverClient:
                def __init__(self) -> None:
                    self.calls = 0
                    self.seen_messages: List[List[Dict[str, Any]]] = []
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> Any:
                    self.calls += 1
                    msgs = kwargs["messages"]
                    self.seen_messages.append(list(msgs))
                    if self.calls <= 2:
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {
                                                    "id": f"bad_{self.calls}",
                                                    "type": "function",
                                                    "function": {
                                                        "name": "read_file",
                                                        "arguments": "{not json",
                                                    },
                                                }
                                            ],
                                        },
                                        "finish_reason": "tool_calls",
                                    }
                                ]
                            }
                        )
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Recovered cleanly.",
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        }
                    )

            mock_client = _ErrorThenRecoverClient()
            breaker = CircuitBreaker(max_failures=2)
            runtime_cfg = {
                "model": "gemini-3.8-flash",
                "max_output_tokens": 1024,
                "effort": "high",
                "cache_mark": False,
                "max_failures": 2,
            }
            answer = await serve.agent(
                fake_session,  # type: ignore[arg-type]
                serve.conversation("Work in {workdir}", fake_session, "Read a file."),  # type: ignore[arg-type]
                serve.TOOLS,
                60.0,
                client=mock_client,
                runtime_cfg=runtime_cfg,
                breaker=breaker,
                final=True,
            )
            self.assertEqual(answer, "Recovered cleanly.")
            self.assertFalse(breaker.locked)
            self.assertEqual(breaker.failure_count, 0)
            self.assertTrue(
                any("circuit breaker tripped" in entry.lower() for entry in fake_session.logs)
            )
            last_prompt_roles = [m.get("role") for m in mock_client.seen_messages[-1]]
            self.assertIn("user", last_prompt_roles[-2:])

        asyncio.run(_scenario())

    def test_cli_parse_args_defaults_and_overrides(self):
        with patch.dict(os.environ, {"PORT": ""}, clear=False):
            defaults = serve.parse_args([])
            self.assertEqual(defaults.host, "0.0.0.0")
            self.assertEqual(defaults.port, 8765)
            self.assertEqual(defaults.config, "submission.yaml")
            self.assertFalse(defaults.dry_run)

        with patch.dict(os.environ, {"PORT": "9123"}, clear=False):
            from_env = serve.parse_args([])
            self.assertEqual(from_env.port, 9123)

        custom = serve.parse_args(
            ["--host", "127.0.0.1", "--port", "9001", "--config", "custom.yaml", "--dry-run"]
        )
        self.assertEqual(custom.host, "127.0.0.1")
        self.assertEqual(custom.port, 9001)
        self.assertEqual(custom.config, "custom.yaml")
        self.assertTrue(custom.dry_run)


if __name__ == "__main__":
    unittest.main(verbosity=2)
