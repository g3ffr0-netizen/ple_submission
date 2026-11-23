"""
Comprehensive Stress & Edge-Case Test Suite for ple_submission.

Covers 5 stress areas:
  1. Circuit Breaker & Lockout
  2. Fallback Rotation & Retry Feedback
  3. Hostile Output Parsing (extract_json_payload & make_verify_fn)
  4. PLE Serve Protocol v1 Concurrency, Fuzzing & Tool Breaker Stress Tests
  5. Config & Eval Resilience (harness.py & eval/run.py --dry-run on 25 mixed tasks)
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import websockets

from eval.run import build_task_verifier, evaluate_tasks, make_dry_run_generate_fn
from harness import (
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    CircuitBreaker,
    HarnessExecutionEngine,
    extract_json_payload,
    load_submission_config,
    make_verify_fn,
)
import serve
import taskbridge


# ---------------------------------------------------------------------------
# 1. Circuit Breaker & Lockout
# ---------------------------------------------------------------------------
class TestStressCircuitBreaker(unittest.TestCase):
    def test_hammer_past_max_failures_locks_and_blocks_calls(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=2,
            max_failures=3,
        )
        gen_calls = 0
        verify_calls = 0

        def always_fail(prompt: str, model: str, max_tokens: int) -> str:
            nonlocal gen_calls
            gen_calls += 1
            return "bad"

        def fail_verify(output: str):
            nonlocal verify_calls
            verify_calls += 1
            return False, "intentional failure"

        statuses = []
        for _ in range(20):
            res = engine.run_with_verification("stress task", always_fail, fail_verify)
            statuses.append(res["status"])

        self.assertEqual(
            statuses[:3],
            [
                "verification_retries_exhausted",
                "verification_retries_exhausted",
                "locked",
            ],
        )
        self.assertTrue(all(s == "locked" for s in statuses[3:]))
        self.assertTrue(engine.locked)
        self.assertEqual(engine.failure_count, 3)
        self.assertEqual(gen_calls, 6)
        self.assertEqual(verify_calls, 6)

    def test_success_before_lockout_resets_failure_streak_and_recovery_after_reset(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=1,
            max_failures=3,
        )
        for _ in range(2):
            r = engine.run_with_verification("t", lambda *_: "bad", lambda _: (False, "no"))
            self.assertFalse(r["ok"])
            self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 2)

        ok_r = engine.run_with_verification("t", lambda *_: "good", lambda _: (True, "ok"))
        self.assertTrue(ok_r["ok"])
        self.assertEqual(engine.failure_count, 0)
        self.assertFalse(engine.locked)

        for _ in range(3):
            engine.run_with_verification("t", lambda *_: "bad", lambda _: (False, "no"))
        self.assertTrue(engine.locked)

        reset_info = engine.reset()
        self.assertEqual(reset_info["status"], "reset")
        self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 0)

        recovered = engine.run_with_verification("t", lambda *_: "good", lambda _: (True, "ok"))
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["status"], "verified")

    def test_non_string_and_none_prompts_do_not_crash(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=1, max_failures=2
        )
        for hostile_prompt in [None, "", "   \n\t  ", 12345, ["prompt"], {"p": 1}]:
            with self.subTest(prompt=hostile_prompt):
                res = engine.run_with_verification(
                    hostile_prompt,  # type: ignore[arg-type]
                    lambda *_: "ok",
                    lambda _: (True, "ok"),
                )
                self.assertIsInstance(res, dict)
                self.assertIn("ok", res)


# ---------------------------------------------------------------------------
# 2. Fallback Rotation & Retry Feedback
# ---------------------------------------------------------------------------
class TestStressFallbackAndRetry(unittest.TestCase):
    def test_rotates_all_submission_yaml_models_across_mixed_failures_no_infinite_loop(self):
        engine = HarnessExecutionEngine(
            config_path=str(REPO_ROOT / "submission.yaml"),
            max_retries=6,
            max_failures=3,
        )
        expected_models = [
            "gemini-4-argon",
            "gemini-3.1-pro-preview",
            "gemini-3.8-flash",
            "gemini-3.1-pro-preview",
            "gemini-3.8-flash",
            "gemini-3.1-pro-preview",
        ]
        seen_models: List[str] = []
        seen_prompts: List[str] = []

        def flaky_generate(prompt: str, model: str, max_tokens: int) -> Any:
            seen_models.append(model)
            seen_prompts.append(prompt)
            attempt = len(seen_models)
            if attempt == 1:
                raise TimeoutError("upstream deadline exceeded")
            if attempt == 2:
                raise RuntimeError("HTTP 503 overloaded")
            if attempt == 3:
                return ""
            if attempt == 4:
                return None
            if attempt == 5:
                return '{"status": "incomplete", "note": "missing phrase"}'
            return '{"status": "ok", "signal": "microgrid verified"}'

        verify = make_verify_fn(
            min_chars=25, required_phrase="microgrid verified", require_json=True
        )
        res = engine.run_with_verification("Check microgrid", flaky_generate, verify)

        self.assertTrue(res["ok"])
        self.assertEqual(res["attempt"], 6)
        self.assertEqual(seen_models, expected_models)
        self.assertEqual(len(res["history"]), 6)

        self.assertIn("upstream deadline exceeded", seen_prompts[1])
        self.assertIn("HTTP 503 overloaded", seen_prompts[2])
        self.assertIn("Output is empty", seen_prompts[3])
        self.assertIn("Output is empty", seen_prompts[4])
        self.assertIn("microgrid verified", seen_prompts[5].lower())
        self.assertLess(len(seen_prompts[5]), 400)

    def test_verifier_exception_is_caught_cleanly(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=2, max_failures=2
        )

        def exploding_verify(_out: str):
            raise ValueError("verifier boom")

        res = engine.run_with_verification("test", lambda *_: "output", exploding_verify)
        self.assertFalse(res["ok"])
        self.assertIn("verifier boom", res["last_reason"])
        self.assertEqual(len(res["history"]), 2)

    def test_elapsed_ms_is_reported_by_engine(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=1, max_failures=2
        )
        res = engine.run_with_verification("test", lambda *_: "ok", lambda _: (True, "ok"))
        self.assertIn("elapsed_ms", res)
        self.assertIsInstance(res["elapsed_ms"], (int, float))


# ---------------------------------------------------------------------------
# 3. Hostile Output Parsing (extract_json_payload & make_verify_fn)
# ---------------------------------------------------------------------------
class TestStressHostileOutputParsing(unittest.TestCase):
    def test_basic_hostile_inputs_never_crash(self):
        verify = make_verify_fn(min_chars=5, required_phrase="ok", require_json=True)
        hostile_outputs: List[Any] = [
            "",
            "   \n\t\r  ",
            None,
            "{",
            '{"a": [1, 2',
            '{"a": NaN, bad}',
            "```json\n```",
            "```\nnot json\n```",
            '```json\n{"ok": true}',
            "\x00\x01\x02\x1f{\"ok\": true}\x00",
            "🚀✨\u202e{\"status\": \"ok\", \"rtl\": \"مرحبا\", \"cjk\": \"日本語\"}",
        ]
        for sample in hostile_outputs:
            with self.subTest(sample=repr(sample)[:40]):
                ok, reason = verify(sample)
                self.assertIsInstance(ok, bool)
                self.assertIsInstance(reason, str)

    def test_non_string_outputs_never_crash(self):
        verify = make_verify_fn(min_chars=1, required_phrase="", require_json=True)
        for sample in [12345, {"a": 1}, ["x"], b'{"a": 1}']:
            with self.subTest(sample=type(sample).__name__):
                payload = extract_json_payload(sample)  # type: ignore[arg-type]
                self.assertIsInstance(payload, str)
                ok, reason = verify(sample)  # type: ignore[arg-type]
                self.assertIsInstance(ok, bool)
                self.assertIsInstance(reason, str)

    def test_deeply_nested_json_does_not_raise_uncaught_recursion_error(self):
        from unittest.mock import patch

        verify = make_verify_fn(min_chars=1, required_phrase="", require_json=True)
        valid_deep = "[" * 50 + "1" + "]" * 50
        ok_50, _ = verify(valid_deep)
        self.assertTrue(ok_50)

        extreme_deep = "[" * 2500 + "1" + "]" * 2500
        ok_ext, reason_ext = verify(extreme_deep)
        self.assertIsInstance(ok_ext, bool)
        self.assertIsInstance(reason_ext, str)

        with patch(
            "harness.json.loads",
            side_effect=RecursionError("maximum recursion depth exceeded"),
        ):
            ok_rec, reason_rec = verify('{"a": 1}')
        self.assertFalse(ok_rec)
        self.assertTrue(
            "nesting" in reason_rec.lower() or "depth" in reason_rec.lower(),
            f"Unexpected RecursionError reason: {reason_rec}",
        )

    def test_multiple_code_blocks_and_prose_wrapped_fences(self):
        verify = make_verify_fn(min_chars=5, required_phrase="ok", require_json=True)

        prose_wrapped = (
            'Sure! Here is the result:\n```json\n{"status": "ok"}\n```\nHope that helps!'
        )
        ok_a, reason_a = verify(prose_wrapped)
        self.assertTrue(ok_a, f"Failed on prose-wrapped JSON fence: {reason_a}")

        multi_block = '```json\n{"status": "ok"}\n```\n\n```text\nextra notes\n```'
        ok_b, reason_b = verify(multi_block)
        self.assertTrue(ok_b, f"Failed on multi-fence output: {reason_b}")

    def test_huge_100kb_outputs_fast_and_safe(self):
        verify = make_verify_fn(
            min_chars=10, required_phrase="sentinel_ok", require_json=True
        )
        big_valid = json.dumps({"marker": "sentinel_ok", "blob": "A" * 110_000})
        big_invalid = '{"marker": "sentinel_ok", "blob": "' + ("B" * 110_000)

        t0 = time.perf_counter()
        ok_v, _ = verify(big_valid)
        ok_i, reason_i = verify(big_invalid)
        elapsed = time.perf_counter() - t0

        self.assertTrue(ok_v)
        self.assertFalse(ok_i)
        self.assertIn("json", reason_i.lower())
        self.assertLess(elapsed, 1.0)


# ---------------------------------------------------------------------------
# 4. PLE Serve Protocol v1 Concurrency, Fuzzing & Tool Breaker Stress Tests
# ---------------------------------------------------------------------------
class TestStressPLEProtocolV1(unittest.TestCase):
    def test_concurrent_protocol_v1_connections_and_fuzzed_frames(self):
        async def _drive_valid(uri: str, idx: int) -> str:
            files: Dict[str, bytes] = {}
            async with websockets.connect(
                uri, max_size=taskbridge.MAX_FRAME, ping_interval=None
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "task",
                            "protocol": 1,
                            "session": f"stress-{idx}",
                            "instruction": f"Task {idx}",
                            "workdir": "/app",
                            "limits": {"agent_timeout_sec": 120},
                        }
                    )
                )
                while True:
                    msg = json.loads(await ws.recv())
                    mtype = msg.get("type")
                    if mtype in ("log", "usage"):
                        continue
                    if mtype in ("done", "error"):
                        return str(mtype)
                    rid = msg["id"]
                    if mtype == "exec":
                        reply = {
                            "type": "result",
                            "id": rid,
                            "ok": True,
                            "exit_code": 0,
                            "output": "/app\n",
                            "truncated": False,
                            "timed_out": False,
                        }
                    elif mtype == "write_file":
                        data = base64.b64decode(msg["content_b64"])
                        files[msg["path"]] = data
                        reply = {"type": "result", "id": rid, "ok": True, "size": len(data)}
                    elif mtype == "read_file":
                        data = files.get(msg["path"], b"HELLO\n")
                        reply = {
                            "type": "result",
                            "id": rid,
                            "ok": True,
                            "content_b64": base64.b64encode(data).decode(),
                            "size": len(data),
                            "truncated": False,
                        }
                    else:
                        reply = {"type": "result", "id": rid, "ok": False, "error": "bad"}
                    await ws.send(json.dumps(reply))

        async def _drive_hostile(uri: str, frame: str) -> str:
            async with websockets.connect(uri) as ws:
                if frame == "__DISCONNECT__":
                    return "disconnected"
                await ws.send(frame)
                msg = json.loads(await ws.recv())
                return str(msg.get("type"))

        async def _scenario() -> None:
            server = await serve.start_ple_server(
                host="127.0.0.1",
                port=0,
                config_path=str(REPO_ROOT / "submission.yaml"),
                dry_run=True,
            )
            try:
                port = server.sockets[0].getsockname()[1]
                uri = f"ws://127.0.0.1:{port}"
                tasks = []
                for i in range(8):
                    tasks.append(_drive_valid(uri, i))
                hostile_frames = [
                    "{malformed json",
                    "null",
                    "[1, 2, 3]",
                    '{"type": "status"}',
                    '{"type": "task", "instruction": 123}',
                    "",
                    "__DISCONNECT__",
                    '{"type": "cancel", "reason": "early"}',
                ]
                for frame in hostile_frames * 2:
                    tasks.append(_drive_hostile(uri, frame))

                outcomes = await asyncio.gather(*tasks)
                self.assertEqual(outcomes[:8], ["done"] * 8)
                for res in outcomes[8:]:
                    self.assertIn(res, {"error", "disconnected"})
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_scenario())

    def test_hostile_run_tool_arguments_and_tool_circuit_breaker(self):
        class _DummySession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}
            mcp_servers: List[Dict[str, Any]] = []

            async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                return {"exit_code": 0, "output": command}

            async def read_file(self, path: str) -> bytes:
                raise taskbridge.TaskError(f"missing {path}")

            async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
                return len(data)

        async def _scenario() -> None:
            sess = _DummySession()
            hostile_calls = [
                {"name": "bash", "arguments": "{bad"},
                {"name": "bash", "arguments": '{"command": NaN}'},
                {"name": "bash", "arguments": "[1, 2, 3]"},
                {"name": "bash", "arguments": '{"command": ""}'},
                {"name": "read_file", "arguments": '{"path": ""}'},
                {"name": "read_file", "arguments": '{"path": "missing.txt"}'},
                {"name": "write_file", "arguments": '{"path": "/app/a.txt"}'},
                {"name": "edit_file", "arguments": '{"path": "x.py", "old_text": "", "new_text": "y"}'},
                {"name": "nonexistent_tool", "arguments": "{}"},
                {"name": "bash", "arguments": '{"command": "' + ("x" * 25000) + '"}'},
            ]
            breaker = CircuitBreaker(max_failures=3)
            trips = 0
            for call in hostile_calls:
                out = await serve.run_tool(sess, dict(call))  # type: ignore[arg-type]
                self.assertIsInstance(out, str)
                if out.startswith("error:"):
                    if breaker.record_failure():
                        trips += 1
                        breaker.reset()
                else:
                    breaker.record_success()
            self.assertGreaterEqual(trips, 2)

        asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# 5. Config & Eval Resilience
# ---------------------------------------------------------------------------
class TestStressConfigAndEval(unittest.TestCase):
    def test_malformed_and_hostile_submission_yaml_configs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            self.assertEqual(load_submission_config(str(tmp / "does_not_exist.yaml")), {})
            self.assertEqual(load_submission_config(str(tmp)), {})

            empty_f = tmp / "empty.yaml"
            empty_f.write_text("", encoding="utf-8")
            e_empty = HarnessExecutionEngine(config_path=str(empty_f))
            self.assertEqual(e_empty.model, DEFAULT_MODEL)
            self.assertEqual(
                e_empty.fallback_models,
                [m for m in DEFAULT_FALLBACK_MODELS if m != e_empty.model],
            )

            bad_yaml = tmp / "bad.yaml"
            bad_yaml.write_text(
                "model: [unclosed\n  harness: : :\n\t::invalid", encoding="utf-8"
            )
            e_bad = HarnessExecutionEngine(config_path=str(bad_yaml))
            self.assertEqual(e_bad.model, DEFAULT_MODEL)

            list_yaml = tmp / "list.yaml"
            list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
            self.assertEqual(load_submission_config(str(list_yaml)), {})

            hostile_cfg = tmp / "hostile.yaml"
            hostile_cfg.write_text(
                json.dumps(
                    {
                        "model": 12345,
                        "harness": {
                            "fallback_models": "not-a-list",
                            "max_retries": True,
                            "max_failures": -99,
                            "max_output_tokens": "NaN",
                        },
                    }
                ),
                encoding="utf-8",
            )
            e_hostile = HarnessExecutionEngine(config_path=str(hostile_cfg))
            self.assertEqual(e_hostile.model, DEFAULT_MODEL)
            self.assertEqual(e_hostile.fallback_models, [])
            self.assertEqual(e_hostile.max_retries, 3)
            self.assertEqual(e_hostile.max_failures, 1)
            self.assertEqual(e_hostile.max_output_tokens, 4096)

    def test_eval_dry_run_25_mixed_pass_fail_tasks_and_lockout_recovery(self):
        tasks: List[Dict[str, Any]] = []
        for i in range(15):
            tasks.append(
                {
                    "id": f"pass_{i:02d}",
                    "prompt": f"Complete passing task {i}",
                    "min_chars": 15,
                    "required_terms": ["verified"],
                    "require_json": (i % 3 == 0),
                }
            )
        for j in range(10):
            tasks.append(
                {
                    "id": f"fail_{j:02d}",
                    "prompt": f"Complete failing task {j}",
                    "min_chars": 30,
                    "required_terms": ["impossible_token_xyz"],
                    "require_json": (j % 2 == 0),
                    "mock_output": "too short",
                }
            )
        mixed_tasks: List[Dict[str, Any]] = []
        mixed_tasks.extend(tasks[15:19])
        mixed_tasks.extend(tasks[0:8])
        mixed_tasks.extend(tasks[19:25])
        mixed_tasks.extend(tasks[8:15])
        self.assertEqual(len(mixed_tasks), 25)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_path = Path(tmp_dir) / "tasks_25.json"
            out_path = Path(tmp_dir) / "results_25.json"
            tasks_path.write_text(json.dumps({"tasks": mixed_tasks}), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "eval" / "run.py"),
                    "--tasks",
                    str(tasks_path),
                    "--out",
                    str(out_path),
                    "--dry-run",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                proc.returncode, 0, f"eval/run.py failed:\n{proc.stderr}\n{proc.stdout}"
            )
            self.assertTrue(out_path.exists())
            report = json.loads(out_path.read_text(encoding="utf-8"))

            self.assertEqual(report["total_tasks"], 25)
            self.assertEqual(report["passed"], 15)
            self.assertEqual(report["failed"], 10)
            self.assertEqual(report["lockouts"], 3)
            self.assertAlmostEqual(report["pass_rate"], 15 / 25, places=4)

        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=1, max_failures=3
        )

        def run_one(task: Dict[str, Any]) -> Dict[str, Any]:
            return engine.run_with_verification(
                prompt=str(task["prompt"]),
                generate_fn=make_dry_run_generate_fn(task),
                verify_fn=build_task_verifier(task),
            )

        no_reset_report = evaluate_tasks(
            tasks=mixed_tasks,
            run_task_fn=run_one,
            reset_fn=engine.reset,
            reset_on_lockout=False,
        )
        self.assertEqual(no_reset_report["passed"], 0)
        self.assertEqual(no_reset_report["failed"], 25)
        self.assertTrue(engine.locked)
        self.assertEqual(no_reset_report["lockouts"], 23)


if __name__ == "__main__":
    unittest.main(verbosity=2)
