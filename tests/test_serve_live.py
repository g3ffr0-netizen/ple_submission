"""End-to-end live loopback WebSocket and Protocol v1 integration tests for serve.py."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
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
            self.assertEqual(rt3["model"], "gemini-4-argon")
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

    def test_agent_max_turns_triggers_graceful_wrap_up(self):
        class _TurnSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}

            def __init__(self) -> None:
                self.logs: List[str] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> Optional[float]:
                return 3600.0

            async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                return {"exit_code": 0, "output": f"ran:{command}"}

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, *_args: Any) -> None:
                return None

        class _EndlessToolClient:
            def __init__(self) -> None:
                self.calls = 0
                self.tool_choices: List[Any] = []
                self.chat = self
                self.completions = self

            async def create(self, **kwargs: Any) -> Any:
                self.calls += 1
                choice_mode = kwargs.get("tool_choice")
                self.tool_choices.append(choice_mode)
                if choice_mode == "none":
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Wrapped up cleanly after hitting max_turns.",
                                    },
                                    "finish_reason": "stop",
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
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": f"loop_{self.calls}",
                                            "type": "function",
                                            "function": {
                                                "name": "bash",
                                                "arguments": json.dumps({"command": "echo loop"}),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )

        async def _scenario() -> None:
            fake_session = _TurnSession()
            loop_client = _EndlessToolClient()
            runtime_cfg = {
                "model": "gemini-3.8-flash",
                "max_output_tokens": 1024,
                "effort": "high",
                "cache_mark": False,
                "max_failures": 3,
            }
            answer = await serve.agent(
                fake_session,  # type: ignore[arg-type]
                serve.conversation("Work in {workdir}", fake_session, "Run loop."),  # type: ignore[arg-type]
                serve.TOOLS,
                60.0,
                client=loop_client,
                runtime_cfg=runtime_cfg,
                final=True,
                max_turns=3,
            )
            self.assertEqual(answer, "Wrapped up cleanly after hitting max_turns.")
            self.assertEqual(fake_session.final_message, answer)
            self.assertEqual(loop_client.calls, 3)
            self.assertEqual(loop_client.tool_choices, ["auto", "auto", "none"])
            self.assertTrue(any("max_turns=3" in entry for entry in fake_session.logs))

        asyncio.run(_scenario())

    def test_view_image_multimodal_attachment_validation_and_pruning(self):
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        )

        class _ImageSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}

            def __init__(self) -> None:
                self.files: Dict[str, bytes] = {
                    "/app/sheet1.png": png_bytes,
                    "/app/sheet2.png": png_bytes,
                    "/app/sheet3.png": png_bytes,
                    "/app/sheet4.png": png_bytes,
                    "/app/sheet5.png": png_bytes,
                    "/app/blob.bin": b"\x00not-an-image",
                    "/app/huge.png": b"\x89PNG" + (b"x" * (serve.MAX_IMAGE_BYTES + 1)),
                }
                self.logs: List[str] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> Optional[float]:
                return 1800.0

            async def read_file(self, path: str) -> bytes:
                if path not in self.files:
                    raise taskbridge.TaskError(f"missing: {path}")
                return self.files[path]

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, *_args: Any) -> None:
                return None

        class _VisionClient:
            def __init__(self) -> None:
                self.calls = 0
                self.seen_messages: List[List[Dict[str, Any]]] = []
                self.chat = self
                self.completions = self

            async def create(self, **kwargs: Any) -> Any:
                self.calls += 1
                self.seen_messages.append(copy_msgs(kwargs["messages"]))
                if self.calls <= 5:
                    path = f"sheet{self.calls}.png"
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            {
                                                "id": f"img_{self.calls}",
                                                "type": "function",
                                                "function": {
                                                    "name": "view_image",
                                                    "arguments": json.dumps({"path": path}),
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
                                    "content": "Inspected all sheets cleanly.",
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )

        def copy_msgs(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return json.loads(json.dumps(msgs))

        async def _scenario() -> None:
            sess = _ImageSession()
            # read_file on PNG points model to view_image
            hint = await serve.run_tool(
                sess, {"name": "read_file", "arguments": json.dumps({"path": "sheet1.png"})}  # type: ignore[arg-type]
            )
            self.assertIn("view_image", hint)

            # view_image validation errors do not populate sink
            sink: List[Dict[str, Any]] = []
            bad_mime = await serve.run_tool(
                sess,
                {"name": "view_image", "arguments": json.dumps({"path": "blob.bin"})},  # type: ignore[arg-type]
                image_sink=sink,
            )
            too_big = await serve.run_tool(
                sess,
                {"name": "view_image", "arguments": json.dumps({"path": "huge.png"})},  # type: ignore[arg-type]
                image_sink=sink,
            )
            self.assertTrue(bad_mime.startswith("error:"))
            self.assertTrue(too_big.startswith("error:"))
            self.assertEqual(sink, [])

            client = _VisionClient()
            messages = serve.conversation("Use view_image in {workdir}.", sess, "Inspect 5 sheets.")  # type: ignore[arg-type]
            runtime_cfg = {
                "model": "gemini-3.8-flash",
                "max_output_tokens": 1024,
                "effort": "high",
                "cache_mark": False,
                "max_failures": 3,
            }
            answer = await serve.agent(
                sess,  # type: ignore[arg-type]
                messages,
                serve.TOOLS,
                60.0,
                client=client,
                runtime_cfg=runtime_cfg,
                max_turns=8,
            )
            self.assertEqual(answer, "Inspected all sheets cleanly.")
            # Every tool result remains plain text and user image messages use data:image/png;base64,...
            tool_msgs = [m for m in client.seen_messages[-1] if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 5)
            self.assertTrue(all(isinstance(m.get("content"), str) for m in tool_msgs))

            active_urls = [
                p
                for m in client.seen_messages[-1]
                if m.get("role") == "user" and isinstance(m.get("content"), list)
                for p in m["content"]
                if isinstance(p, dict) and p.get("type") == "image_url"
            ]
            self.assertEqual(len(active_urls), serve.MAX_ACTIVE_IMAGES)
            self.assertTrue(
                active_urls[-1]["image_url"]["url"].startswith("data:image/png;base64,")
            )
            omitted_texts = [
                p.get("text", "")
                for m in client.seen_messages[-1]
                if m.get("role") == "user" and isinstance(m.get("content"), list)
                for p in m["content"]
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            self.assertTrue(any("Earlier image omitted" in t for t in omitted_texts))

            # shorten() frees old images cleanly when recovering from context overflow
            pruned = await serve.shorten(sess, messages)  # type: ignore[arg-type]
            self.assertTrue(pruned)
            self.assertTrue(any("pruned" in entry for entry in sess.logs))

        asyncio.run(_scenario())

    def test_agent_turn_pacing_nudges_and_urgent_warning(self):
        class _PacingSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}

            def __init__(self) -> None:
                self.logs: List[str] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> Optional[float]:
                return 3600.0

            async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                return {"exit_code": 0, "output": f"ran:{command}"}

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, *_args: Any) -> None:
                return None

        class _PacingClient:
            def __init__(self) -> None:
                self.calls = 0
                self.seen_tool_outputs: List[str] = []
                self.chat = self
                self.completions = self

            async def create(self, **kwargs: Any) -> Any:
                self.calls += 1
                msgs = kwargs["messages"]
                if msgs and msgs[-1].get("role") == "tool":
                    self.seen_tool_outputs.append(str(msgs[-1]["content"]))
                if kwargs.get("tool_choice") == "none":
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Delivered and wrapped up.",
                                    },
                                    "finish_reason": "stop",
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
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": f"step_{self.calls}",
                                            "type": "function",
                                            "function": {
                                                "name": "bash",
                                                "arguments": json.dumps({"command": "pwd"}),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )

        async def _scenario() -> None:
            sess = _PacingSession()
            client = _PacingClient()
            runtime_cfg = {
                "model": "gemini-3.8-flash",
                "max_output_tokens": 1024,
                "effort": "high",
                "cache_mark": False,
                "max_failures": 3,
            }
            answer = await serve.agent(
                sess,  # type: ignore[arg-type]
                serve.conversation("Work in {workdir}", sess, "Build deliverable."),  # type: ignore[arg-type]
                serve.TOOLS,
                60.0,
                client=client,
                runtime_cfg=runtime_cfg,
                final=True,
                max_turns=10,
            )
            self.assertEqual(answer, "Delivered and wrapped up.")
            self.assertEqual(client.calls, 10)
            self.assertIn("[Turn 1/10 (8 tool turn(s) left before wrap-up)", client.seen_tool_outputs[0])
            self.assertIn("past halfway through turn budget", client.seen_tool_outputs[4])
            self.assertIn("[URGENT: only 3 tool turn(s) left", client.seen_tool_outputs[5])
            self.assertIn("[URGENT: only 1 tool turn(s) left", client.seen_tool_outputs[7])

        asyncio.run(_scenario())

    def test_truncated_verifier_never_passes_and_forces_fix_round(self):
        class _ScriptedSession:
            instruction = "Produce /app/deliverable.txt"
            workdir = "/app"
            mcp_servers: List[Dict[str, Any]] = []
            limits = {"exec_timeout_sec": 60}

            def __init__(self) -> None:
                self.files: Dict[str, bytes] = {}
                self.logs: List[str] = []
                self.usages: List[tuple] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> float:
                return 3600.0

            async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                return {"exit_code": 0, "output": "", "truncated": False}

            async def read_file(self, path: str) -> bytes:
                if path not in self.files:
                    raise taskbridge.TaskError(f"missing {path}")
                return self.files[path]

            async def write_file(self, path: str, data: Any, append: bool = False) -> int:
                raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                self.files[path] = (self.files.get(path, b"") + raw) if append else raw
                return len(raw)

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, model: str, inp: int, out: int, cached: int = 0) -> None:
                self.usages.append((model, inp, out, cached))

        class _TruncatingVerifierClient:
            def __init__(self) -> None:
                self.verify_round = 0
                self.verify_turns = 0
                self.saw_verifier_wrap_up = False
                self.fix_rounds = 0
                self.chat = self
                self.completions = self

            async def create(self, **kwargs: Any) -> serve._DryRunObject:
                msgs = kwargs["messages"]
                sys_msg = str(msgs[0].get("content") or "")
                user_texts = [
                    str(m.get("content") or "")
                    for m in msgs
                    if m.get("role") == "user" and isinstance(m.get("content"), str)
                ]
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                def _reply(content: Optional[str], calls: Optional[List[Dict[str, Any]]] = None) -> serve._DryRunObject:
                    msg: Dict[str, Any] = {"role": "assistant", "content": content}
                    if calls:
                        msg["tool_calls"] = calls
                    return serve._DryRunObject(
                        {
                            "choices": [{"finish_reason": "stop", "message": msg}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                        }
                    )

                if sys_msg.startswith("You plan an engineering task"):
                    return _reply("Plan: write /app/deliverable.txt")

                if sys_msg.startswith("You extract strict engineering constraints"):
                    return _reply(json.dumps({"source": "llm_extractor", "deliverables": ["/app/deliverable.txt"]}))

                if sys_msg.startswith("You check an engineer's work"):
                    last_user = user_texts[-1] if user_texts else ""
                    if serve.VERIFIER_WRAP_UP["content"] in last_user:
                        self.saw_verifier_wrap_up = True
                        # Reproduce premature max_turns PASS bug: solve() MUST reject this!
                        return _reply("PASS")
                    if not tool_msgs:
                        self.verify_round += 1
                        self.verify_turns = 0
                    self.verify_turns += 1
                    if self.verify_round == 1:
                        return _reply(
                            None,
                            [
                                {
                                    "id": f"v_r1_{self.verify_turns}",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "/app/deliverable.txt"}),
                                    },
                                }
                            ],
                        )
                    if self.verify_turns == 1:
                        return _reply(
                            None,
                            [
                                {
                                    "id": "v_r2_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "/app/deliverable.txt"}),
                                    },
                                }
                            ],
                        )
                    return _reply("PASS")

                # Worker or Fix round
                in_fix = any("A reviewer checked your files" in t for t in user_texts)
                if in_fix:
                    last_gap = next(
                        (t for t in reversed(user_texts) if "A reviewer checked your files" in t),
                        "",
                    )
                    if not any(
                        m.get("role") == "assistant"
                        and any(
                            c.get("id") == "fix_write_1"
                            for c in (m.get("tool_calls") or [])
                            if isinstance(c, dict)
                        )
                        for m in msgs
                    ):
                        self.fix_rounds += 1
                        assert "out of turns/time" in last_gap
                        return _reply(
                            None,
                            [
                                {
                                    "id": "fix_write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps(
                                            {"path": "/app/deliverable.txt", "content": "FIXED"}
                                        ),
                                    },
                                }
                            ],
                        )
                    return _reply("Updated /app/deliverable.txt cleanly.")

                if not tool_msgs:
                    return _reply(
                        None,
                        [
                            {
                                "id": "work_write_1",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": "/app/deliverable.txt", "content": "INITIAL"}
                                    ),
                                },
                            }
                        ],
                    )
                return _reply("Wrote /app/deliverable.txt")

        async def _scenario() -> None:
            sess = _ScriptedSession()
            client = _TruncatingVerifierClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "gemini-4-argon",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "test-key",
                    "MAX_FIX_ROUNDS": "2",
                    "MAX_PLAN_TURNS": "3",
                    "MAX_WORK_TURNS": "5",
                    "MAX_VERIFY_TURNS": "3",
                    "MAX_FIX_TURNS": "5",
                },
                clear=False,
            ):
                answer = await serve.solve(
                    sess,  # type: ignore[arg-type]
                    client=client,
                    config_path=str(REPO_ROOT / "submission.yaml"),
                )
            self.assertEqual(answer, "Updated /app/deliverable.txt cleanly.")
            self.assertEqual(sess.files.get("/app/deliverable.txt"), b"FIXED")
            self.assertEqual(client.verify_round, 2)
            self.assertEqual(client.fix_rounds, 1)
            self.assertTrue(client.saw_verifier_wrap_up)
            self.assertTrue(any("verifier: truncated=True" in entry for entry in sess.logs))

        asyncio.run(_scenario())

    def test_programmatic_audit_detects_csv_shift_and_road_building_crossing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "work"
            pkgdir = tmp_path / "fake_pkgs"
            workdir.mkdir()
            (pkgdir / "ifcopenshell").mkdir(parents=True)

            # 1. CSV with shifted row, missing register ID, and #REF! error token + invalid JSON
            (workdir / "devices.csv").write_text(
                "device_id,register,value\n"
                "DEV1,40001,10,EXTRA_SHIFT\n"
                "DEV2,,20\n"
                "DEV3,40003,#REF!\n",
                encoding="utf-8",
            )
            (workdir / "bad.json").write_text('{"x": NaN}', encoding="utf-8")
            (workdir / "site.ifc").write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")

            # Minimal fake ifcopenshell package (with ifcopenshell.geom submodule)
            fake_ifc = textwrap.dedent(
                """
                class _Entity:
                    def __init__(self, eid, cls, gid=None, name="", obj_type="", repr_=True):
                        self._eid = eid
                        self._cls = cls
                        self.GlobalId = gid or f"G_{eid}"
                        self.Name = name
                        self.ObjectType = obj_type
                        self.Representation = repr_
                        self.IsDecomposedBy = []
                        self.ContainsElements = []
                    def id(self):
                        return self._eid
                    def is_a(self, target=None):
                        return self._cls == target if target else self._cls
                    def __str__(self):
                        return f"#{self._eid}={self._cls}"

                class _LineIdx:
                    def __init__(self, idxs):
                        self._idxs = idxs
                    def is_a(self, target=None):
                        return target == "IfcLineIndex"
                    def __getitem__(self, item):
                        return self._idxs

                class _PolyCurve(_Entity):
                    def __init__(self):
                        super().__init__(99, "IfcIndexedPolyCurve", repr_=False)
                        self.Points = type("_Pts", (), {"CoordList": [(0, 0), (10, 0), (10, 10)]})()
                        self.Segments = [_LineIdx((1, 2, 3, 1, 1))]

                class _Model:
                    def __init__(self):
                        self.bldg = _Entity(1, "IfcBuilding", gid="BLDG_BRM", name="BRM", repr_=False)
                        self.bldg_slab = _Entity(2, "IfcSlab", gid="SLAB_BRM", name="BRM_SLAB")
                        self.bldg.ContainsElements = [type("_Rel", (), {"RelatedElements": [self.bldg_slab]})()]
                        self.road = _Entity(3, "IfcCivilElement", gid="ROAD_1", name="ROAD_P1_1")
                        self.curve = _PolyCurve()

                    def by_type(self, name):
                        if name == "IfcIndexedPolyCurve":
                            return [self.curve]
                        if name == "IfcBuilding":
                            return [self.bldg]
                        if name == "IfcProduct":
                            return [self.bldg, self.bldg_slab, self.road]
                        return []

                def open(_path):
                    return _Model()
                """
            ).lstrip()
            fake_geom = textwrap.dedent(
                """
                class settings:
                    USE_WORLD_COORDS = "use-world-coords"
                    def set(self, k, v):
                        pass

                def create_shape(_settings, elem):
                    def _box(x0, x1, y0, y1, z0, z1):
                        verts = [
                            x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
                            x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1,
                        ]
                        faces = [
                            0, 1, 2, 0, 2, 3,
                            4, 5, 6, 4, 6, 7,
                            0, 1, 5, 0, 5, 4,
                            1, 2, 6, 1, 6, 5,
                            2, 3, 7, 2, 7, 6,
                            3, 0, 4, 3, 4, 7,
                        ]
                        return type("_Shape", (), {"geometry": type("_Geo", (), {"verts": verts, "faces": faces})()})()
                    if elem.GlobalId == "SLAB_BRM":
                        return _box(10.0, 20.0, 10.0, 20.0, 0.0, 5.0)
                    if elem.GlobalId == "ROAD_1":
                        return _box(0.0, 30.0, 12.0, 16.0, 0.0, 0.2)
                    return None
                """
            ).lstrip()
            (pkgdir / "ifcopenshell" / "__init__.py").write_text(fake_ifc, encoding="utf-8")
            (pkgdir / "ifcopenshell" / "geom.py").write_text(fake_geom, encoding="utf-8")

            env = dict(os.environ)
            env["PYTHONPATH"] = str(pkgdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            proc = subprocess.run(
                [sys.executable, "-"],
                input=serve._build_programmatic_audit_script(),
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            audit_line = next(
                line for line in proc.stdout.splitlines() if line.startswith(serve.AUDIT_MARKER)
            )
            parsed = json.loads(audit_line[len(serve.AUDIT_MARKER):])
            joined = "\n".join(parsed["errors"])
            self.assertIn("shifted/malformed CSV columns", joined)
            self.assertIn("missing/placeholder IDs", joined)
            self.assertIn("invalid numeric/formula token '#REF!'", joined)
            self.assertIn("invalid JSON or non-finite numeric literal", joined)
            self.assertIn("consecutive duplicate IfcLineIndex index 1", joined)
            self.assertIn("road 'ROAD_P1_1' intersects building 'BRM (site.ifc)'", joined)

            # 2. Verify solve() refuses non-truncated VERIFIER PASS while audit errors exist, then passes after fix
            (workdir / "bad.json").write_text('{"ok": true}', encoding="utf-8")
            (workdir / "site.ifc").unlink()
            clean_csv = "device_id,register,value\nDEV1,40001,10\nDEV2,40002,20\nTOTAL,,30\n"

            class _AuditSession:
                instruction = "Fix devices.csv"
                mcp_servers: List[Dict[str, Any]] = []
                limits = {"exec_timeout_sec": 60}

                def __init__(self) -> None:
                    self.workdir = str(workdir)
                    self.logs: List[str] = []
                    self.final_message = ""
                    self.cancelled = asyncio.Event()

                def time_left(self) -> float:
                    return 3600.0

                async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                    is_audit = "__PLE_AUDIT__" in command
                    res = subprocess.run(
                        [sys.executable, "-"] if is_audit else ["cmd", "/c", command],
                        input=serve._build_programmatic_audit_script() if is_audit else None,
                        cwd=workdir,
                        capture_output=True,
                        text=True,
                    )
                    return {"exit_code": res.returncode, "output": res.stdout + res.stderr, "truncated": False}

                async def write_file(self, path: str, data: Any, append: bool = False) -> int:
                    target = workdir / Path(path.replace("\\", "/")).name
                    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                    target.write_bytes((target.read_bytes() + raw) if append and target.exists() else raw)
                    return len(raw)

                async def read_file(self, path: str) -> bytes:
                    return (workdir / Path(path.replace("\\", "/")).name).read_bytes()

                async def log(self, text: str) -> None:
                    self.logs.append(text)

                async def usage(self, model: str, inp: int, out: int, cached: int = 0) -> None:
                    pass

            class _AuditClient:
                def __init__(self) -> None:
                    self.seen_audit_precheck = False
                    self.fix_ran = False
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_texts = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    if sys_msg.startswith("You plan an engineering task"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan"}}]}
                        )
                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"source": "llm_extractor", "deliverables": ["devices.csv"]})}}]}
                        )

                    if sys_msg.startswith("You check an engineer's work"):
                        if any("Programmatic Pre-Check" in t for t in user_texts):
                            self.seen_audit_precheck = True
                        # Always try to say PASS: audit error must block PASS on round 1!
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                        )
                    if any("Programmatic Pre-Check ERRORS" in t for t in user_texts):
                        if not self.fix_ran:
                            self.fix_ran = True
                            return serve._DryRunObject(
                                {
                                    "choices": [
                                        {
                                            "finish_reason": "stop",
                                            "message": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [
                                                    {
                                                        "id": "fix_csv",
                                                        "type": "function",
                                                        "function": {
                                                            "name": "write_file",
                                                            "arguments": json.dumps(
                                                                {
                                                                    "path": "devices.csv",
                                                                    "content": clean_csv,
                                                                }
                                                            ),
                                                        },
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Fixed CSV."}}]}
                        )
                    return serve._DryRunObject(
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Initial."}}]}
                    )

            async def _run_with_ref(client_obj: _AuditClient) -> str:
                with patch.dict(
                    os.environ,
                    {
                        "MODEL_NAME": "gemini-4-argon",
                        "OPENAI_BASE_URL": "http://proxy.local/v1",
                        "OPENAI_API_KEY": "test-key",
                        "MAX_FIX_ROUNDS": "2",
                    },
                    clear=False,
                ):
                    return await serve.solve(
                        _AuditSession(),  # type: ignore[arg-type]
                        client=client_obj,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )

            audit_client = _AuditClient()
            ans = asyncio.run(_run_with_ref(audit_client))
            self.assertEqual(ans, "Fixed CSV.")
            self.assertTrue(audit_client.seen_audit_precheck)
            self.assertTrue(audit_client.fix_ran)

    def test_programmatic_audit_mep_bvh_flags_zero_mm_joints_and_allows_6mm_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pkgdir = tmp_path / "fake_pkgs"
            (pkgdir / "ifcopenshell").mkdir(parents=True)

            fake_ifc = textwrap.dedent(
                """
                import os

                class _Entity:
                    def __init__(self, eid, cls, gid, name="", obj_type="", repr_=True):
                        self._eid = eid
                        self._cls = cls
                        self.GlobalId = gid
                        self.Name = name
                        self.ObjectType = obj_type
                        self.Representation = repr_
                        self.IsDecomposedBy = []
                        self.ContainsElements = []
                    def id(self):
                        return self._eid
                    def is_a(self, target=None):
                        return self._cls == target if target else self._cls
                    def __str__(self):
                        return f"#{self._eid}={self._cls}"

                class _RelConnects:
                    def __init__(self, e1, e2):
                        self.RelatingElement = e1
                        self.RelatedElement = e2

                class _Model:
                    def __init__(self, path):
                        clean = "clean" in os.path.basename(path)
                        gap = 0.006 if clean else 0.0
                        tray_ov = 0.006 if clean else -0.0005
                        tray_z0 = 3.18 if clean else 2.996
                        spr_z0 = 2.70 if clean else 1.89

                        self.floor = _Entity(1, "IfcSlab", "FLOOR_1", "ACCESS-FLOOR")
                        self.pdu = _Entity(2, "IfcElectricDistributionBoard", "PDU_1", "PDU-01", "PDU")
                        self.pipe1 = _Entity(3, "IfcPipeSegment", "PIPE_1", "CHW-1-SEG-01")
                        self.pipe2 = _Entity(4, "IfcPipeSegment", "PIPE_2", "CHW-1-SEG-02")
                        self.tray1 = _Entity(5, "IfcCableCarrierSegment", "TRAY_1", "CT-1-SEG-01", "CableTray")
                        self.tray2 = _Entity(6, "IfcCableCarrierSegment", "TRAY_2", "CT-1-SEG-02", "CableTray")
                        self.chw_under = _Entity(7, "IfcPipeSegment", "CHW_U", "CHW-1-UNDER")
                        self.spr = _Entity(8, "IfcFireSuppressionTerminal", "SPR_A1", "SPR-A01", "Sprinkler")

                        self._boxes = {
                            1: (0.0, 12.0, 0.0, 12.0, -0.035, 0.0),
                            2: (10.0, 11.0, 1.0, 2.0, 0.0, 1.96),
                            3: (0.0, 2.0, 1.0, 1.2, 2.70, 2.86),
                            4: (2.0 + gap, 4.994 if clean else 4.0, 1.0, 1.2, 2.70, 2.86),
                            5: (5.0, 7.0, 1.0, 1.6, tray_z0, tray_z0 + 0.127),
                            6: (7.0 + tray_ov, 8.0, 1.0, 1.6, tray_z0, tray_z0 + 0.127),
                            7: (5.0, 7.0, 1.0, 1.2, 2.70, 2.86),
                            8: (9.5, 9.6, 5.0, 5.1, spr_z0, spr_z0 + 0.10),
                        }
                        self.products = [
                            self.floor, self.pdu, self.pipe1, self.pipe2,
                            self.tray1, self.tray2, self.chw_under, self.spr,
                        ]
                        for _p in self.products:
                            _p._box = self._boxes[_p.id()]
                        self.rels = [_RelConnects(self.pipe1, self.pipe2)]

                    def by_type(self, name):
                        if name == "IfcProduct":
                            return self.products
                        if name == "IfcRelConnectsElements":
                            return self.rels
                        return []

                def open(path):
                    return _Model(path)
                """
            ).lstrip()

            fake_geom = textwrap.dedent(
                """
                class settings:
                    USE_WORLD_COORDS = "use-world-coords"
                    def set(self, k, v):
                        pass

                class _Shape:
                    def __init__(self, eid, gid, verts, faces, box):
                        self.id = eid
                        self.guid = gid
                        self.geometry = type("_Geo", (), {"verts": verts, "faces": faces})()
                        self._box = box

                def create_shape(_settings, elem):
                    box = getattr(elem, "_box", None)
                    if box is None:
                        # Look up from caller model boxes stashed via helper or reconstruct on elem
                        raise ValueError("missing box")
                    x0, x1, y0, y1, z0, z1 = box
                    verts = [
                        x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
                        x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1,
                    ]
                    faces = [
                        0, 1, 2, 0, 2, 3,
                        4, 5, 6, 4, 6, 7,
                        0, 1, 5, 0, 5, 4,
                        1, 2, 6, 1, 6, 5,
                        2, 3, 7, 2, 7, 6,
                        3, 0, 4, 3, 4, 7,
                    ]
                    return _Shape(elem.id(), elem.GlobalId, verts, faces, box)

                class tree:
                    def __init__(self):
                        self._items = {}

                    def add_element(self, shape):
                        self._items[shape.id] = shape

                    def _touches_or_overlaps(self, b1, b2, tol=0.0):
                        return (
                            min(b1[1], b2[1]) - max(b1[0], b2[0]) >= -tol
                            and min(b1[3], b2[3]) - max(b1[2], b2[2]) >= -tol
                            and min(b1[5], b2[5]) - max(b1[4], b2[4]) >= -tol
                        )

                    def select(self, elem):
                        s1 = self._items.get(elem.id())
                        if s1 is None:
                            return []
                        hits = []
                        for eid, s2 in self._items.items():
                            if eid != s1.id and self._touches_or_overlaps(s1._box, s2._box, tol=1e-6):
                                hits.append(eid)
                        return hits

                    def clash_intersection_many(self, set_a, set_b, tolerance=0.0, check_all=True, allow_touching=False):
                        out = []
                        ids_b = {e.id() for e in set_b}
                        for ea in set_a:
                            s1 = self._items.get(ea.id())
                            if s1 is None:
                                continue
                            for eb_id in ids_b:
                                if eb_id == s1.id:
                                    continue
                                s2 = self._items.get(eb_id)
                                if s2 and self._touches_or_overlaps(s1._box, s2._box, tol=1e-6):
                                    out.append((s1.id, s2.id))
                        return out
                """
            ).lstrip()
            (pkgdir / "ifcopenshell" / "__init__.py").write_text(fake_ifc, encoding="utf-8")
            (pkgdir / "ifcopenshell" / "geom.py").write_text(fake_geom, encoding="utf-8")

            env = dict(os.environ)
            env["PYTHONPATH"] = str(pkgdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

            def _run_case(dir_name: str, filename: str) -> Dict[str, Any]:
                w = tmp_path / dir_name
                w.mkdir()
                (w / filename).write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-"],
                    input=serve._build_programmatic_audit_script(),
                    cwd=w,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                line = next(l for l in proc.stdout.splitlines() if l.startswith(serve.AUDIT_MARKER))
                return json.loads(line[len(serve.AUDIT_MARKER):])

            bad = _run_case("bad_case", "mep_bad.ifc")
            bad_errs = "\n".join(bad["errors"])
            bad_warns = "\n".join(bad["warnings"])
            self.assertIn("CHW-1-SEG-01", bad_errs)
            self.assertIn("CHW-1-SEG-02", bad_errs)
            self.assertIn("CT-1-SEG-01", bad_errs)
            self.assertIn("CT-1-SEG-02", bad_errs)
            self.assertNotIn("PDU-01", bad_errs)
            self.assertIn("tray-stack vertical headroom 136.0mm < 300mm", bad_errs)
            self.assertIn("overhead clear height 1890.0mm < 2600mm", bad_warns)

            clean = _run_case("clean_case", "mep_clean.ifc")
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
