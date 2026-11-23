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
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import openai
import websockets

from eval.run import build_task_verifier, evaluate_tasks, make_dry_run_generate_fn
from harness import (
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    CircuitBreaker,
    HarnessExecutionEngine,
    classify_transient_error,
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


# ---------------------------------------------------------------------------
# 6. Adversarial Harness, Audit, Turn-Cap & Backoff Stress Tests
# ---------------------------------------------------------------------------
class TestAdversarialHarnessStress(unittest.TestCase):
    @staticmethod
    def _run_audit_subprocess(workdir: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        proc = subprocess.run(
            [sys.executable, "-"],
            input=serve._build_programmatic_audit_script(),
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        line = next(l for l in proc.stdout.splitlines() if l.startswith(serve.AUDIT_MARKER))
        return json.loads(line[len(serve.AUDIT_MARKER):])

    @staticmethod
    def _minimal_xlsx_bytes() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", "<workbook/>")
        return buf.getvalue()

    def test_audit_catches_zipped_csv_schema_drift_units_formula_and_register_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_dir = tmp_path / "bad_pkg"
            (bad_dir / "output").mkdir(parents=True)

            # Loose CSV: register case mismatch REG-01 vs zipped reg-01 + partial set mismatch (REG-03)
            (bad_dir / "loose_summary.csv").write_text(
                "register,length_mm,notes\n"
                "REG-01,100,Install 120 mm tray\n"
                "REG-03,250,See 45 kW feeder\n",
                encoding="utf-8",
            )
            # Corrupt XLSX and corrupt ZIP
            (bad_dir / "broken.xlsx").write_bytes(b"not a zip")
            (bad_dir / "corrupt.zip").write_bytes(b"corrupt zip bytes")

            # Valid ZIP holding a malformed CSV inside output/submission.zip
            zpath = bad_dir / "output" / "submission.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr(
                    "devices.csv",
                    "device_id,register,power,length_mm,power\n"
                    "DEV1,reg-01,10,120 mm,10\n"
                    "DEV2,,20,200,20\n"
                    "DEV3,REG-02,45 kW,=SUM(#REF!),45\n"
                    "DEV4,REG-04,50,150,#VALUE!\n",
                )

            res = self._run_audit_subprocess(bad_dir)
            errs = "\n".join(res["errors"])
            warns = "\n".join(res["warnings"])

            self.assertIn("duplicate column header 'power'", errs)
            self.assertIn("unparsed unit string '120 mm'", errs)
            self.assertIn("unparsed unit string '45 kW'", errs)
            self.assertIn("=SUM(#REF!)", errs)
            self.assertIn("#VALUE!", errs)
            self.assertIn("register/ID case/whitespace mismatch", errs)
            self.assertIn("missing/placeholder IDs", errs)
            self.assertIn("corrupt XLSX/ZIP container", errs)
            self.assertIn("corrupt ZIP archive", errs)
            self.assertIn("cross-table 'register' mismatch", warns)

            # Clean directory (loose + zipped CSV + valid XLSX container + prose column unchanged)
            clean_dir = tmp_path / "clean_pkg"
            (clean_dir / "output").mkdir(parents=True)
            clean_csv = (
                "register,length_mm,notes\n"
                "REG-01,100,Install 120 mm tray\n"
                "REG-02,250,See 45 kW feeder\n"
            )
            (clean_dir / "summary.csv").write_text(clean_csv, encoding="utf-8")
            (clean_dir / "schedule.xlsx").write_bytes(self._minimal_xlsx_bytes())
            with zipfile.ZipFile(clean_dir / "output" / "submission.zip", "w") as zf:
                zf.writestr("devices.csv", clean_csv)
            clean = self._run_audit_subprocess(clean_dir)
            self.assertEqual(clean["errors"], [])

    def test_audit_catches_ifc_invalid_geometry_nan_verts_0mm_joints_micro_clashes_and_road_corner(self):
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

                class _Pt:
                    def __init__(self, coords):
                        self.Coordinates = coords

                class _Polyline:
                    def __init__(self, coords):
                        self.Points = [_Pt(c) for c in coords]
                    def __str__(self):
                        return "#500=IfcPolyline"

                class _Model:
                    def __init__(self, path):
                        clean = "clean" in os.path.basename(path)
                        use_bvh = "with_bvh" in os.path.basename(path)
                        self.use_bvh = use_bvh
                        self.polylines = [_Polyline([(0.0, 0.0), (1.0, 0.0)])] if clean else [_Polyline([(0.0, 0.0)])]

                        self.bldg = _Entity(1, "IfcBuilding", "BLDG_1", "BRM", repr_=False)
                        self.col = _Entity(2, "IfcColumn", "COL_1", "COL-A1")
                        self.bldg.ContainsElements = [type("_Rel", (), {"RelatedElements": [self.col]})()]
                        self.road = _Entity(3, "IfcCivilElement", "ROAD_1", "ROAD_CORNER")
                        self.pipe1 = _Entity(4, "IfcPipeSegment", "PIPE_1", "CHW-1-SEG-01")
                        self.pipe2 = _Entity(5, "IfcPipeSegment", "PIPE_2", "CHW-1-SEG-02")
                        self.tray1 = _Entity(6, "IfcCableCarrierSegment", "TRAY_1", "CT-1-SEG-01", "CableTray")
                        self.spr = _Entity(7, "IfcFireSuppressionTerminal", "SPR_1", "SPR-A01", "Sprinkler")
                        self.bad_brep = _Entity(8, "IfcWall", "BAD_BREP_1", "BAD_BREP")
                        self.nan_elem = _Entity(9, "IfcWall", "NAN_WALL_1", "NAN_WALL")

                        if clean:
                            self._boxes = {
                                2: (6.0, 6.5, 1.0, 1.5, 0.0, 4.0),
                                3: (0.0, 5.0, 0.0, 0.9, 0.0, 0.2),
                                4: (0.0, 2.0, 3.0, 3.2, 2.70, 2.86),
                                5: (2.006, 4.0, 3.0, 3.2, 2.70, 2.86),
                                6: (0.0, 2.0, 3.0, 3.6, 3.18, 3.307),
                                7: (9.0, 9.1, 5.0, 5.1, 2.70, 2.80),
                            }
                            self.products = [self.bldg, self.col, self.road, self.pipe1, self.pipe2, self.tray1, self.spr]
                        else:
                            # col: X=[6.9996, 7.5], Y=[1.0, 1.5]; tray1: X=[5.0, 7.0], Y=[1.0, 1.6] -> 0.4mm micro-clash!
                            # bldg col/footing at [6.0, 7.0] x [1.0, 2.0]; road at [5.0, 6.2] x [0.8, 1.1] -> 0.2*0.1 = 0.02 m^2 crossing!
                            # pipe1 ends X=2.0, pipe2 starts X=2.0 -> 0mm butt-joint caught WITHOUT BVH too!
                            self._boxes = {
                                2: (6.9996, 7.5, 1.0, 1.5, 0.0, 4.0),
                                3: (5.0, 7.2, 0.8, 1.1, 0.0, 0.2),
                                4: (0.0, 2.0, 3.0, 3.2, 2.70, 2.86),
                                5: (2.0, 4.0, 3.0, 3.2, 2.70, 2.86),
                                6: (5.0, 7.0, 1.0, 1.6, 2.996, 3.123),
                                7: (9.0, 9.1, 5.0, 5.1, 1.89, 1.99),
                                8: "RAISE",
                                9: "NAN",
                            }
                            self._boxes[10] = (5.0, 7.0, 1.0, 1.2, 2.70, 2.86)
                            self.chw_u = _Entity(10, "IfcPipeSegment", "CHW_U", "CHW-1-UNDER")
                            self.products = [
                                self.bldg, self.col, self.road, self.pipe1, self.pipe2,
                                self.tray1, self.spr, self.bad_brep, self.nan_elem, self.chw_u,
                            ]
                        for _p in self.products:
                            if _p.id() in self._boxes:
                                _p._box = self._boxes[_p.id()]

                    def by_type(self, name):
                        if name == "IfcPolyline":
                            return self.polylines
                        if name == "IfcBuilding":
                            return [self.bldg]
                        if name == "IfcProduct":
                            return self.products
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

                def create_shape(_settings, elem):
                    box = getattr(elem, "_box", None)
                    if box == "RAISE":
                        raise RuntimeError("corrupt BRep face")
                    if box == "NAN":
                        return type(
                            "_Shape",
                            (),
                            {
                                "id": elem.id(),
                                "guid": elem.GlobalId,
                                "geometry": type(
                                    "_Geo",
                                    (),
                                    {
                                        "verts": [float("nan"), 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                                        "faces": [0, 1, 2],
                                    },
                                )(),
                            },
                        )()
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
                    return type(
                        "_Shape",
                        (),
                        {"id": elem.id(), "guid": elem.GlobalId, "geometry": type("_Geo", (), {"verts": verts, "faces": faces})()},
                    )()
                """
            ).lstrip()

            (pkgdir / "ifcopenshell" / "__init__.py").write_text(fake_ifc, encoding="utf-8")
            (pkgdir / "ifcopenshell" / "geom.py").write_text(fake_geom, encoding="utf-8")

            env = dict(os.environ)
            env["PYTHONPATH"] = str(pkgdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

            bad_dir = tmp_path / "bad_ifc_dir"
            bad_dir.mkdir()
            (bad_dir / "site_mep.ifc").write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")
            bad = self._run_audit_subprocess(bad_dir, env=env)
            bad_errs = "\n".join(bad["errors"])
            bad_warns = "\n".join(bad["warnings"])

            self.assertIn("< 2 points", bad_errs)
            self.assertIn("invalid/unmesheable IFC geometry", bad_errs)
            self.assertIn("non-finite (NaN/Inf) vertex coordinate", bad_errs)
            self.assertIn("MEP touching/overlapping clash (<2mm gap", bad_errs)
            self.assertIn("MEP micro-clash (<1mm clearance or overlapping)", bad_errs)
            self.assertIn("intersects building 'BRM (site_mep.ifc)'", bad_errs)
            self.assertIn("tray-stack vertical headroom 136.0mm < 300mm", bad_errs)
            self.assertIn("overhead clear height 1890.0mm < 2600mm", bad_warns)

            clean_dir = tmp_path / "clean_ifc_dir"
            clean_dir.mkdir()
            (clean_dir / "clean_model.ifc").write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")
            clean = self._run_audit_subprocess(clean_dir, env=env)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])

    def test_turn_budget_exhaustion_worker_and_verifier_never_false_pass_and_passes_audit_errors_to_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "schedule.csv").write_text(
                "device_id,register,length_mm\nD1,,10 mm\n",
                encoding="utf-8",
            )
            fixed_csv = "device_id,register,length_mm\nD1,REG-1,10\n"

            class _StressSession:
                instruction = "Fix schedule.csv cleanly."
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
                        [sys.executable, "-"] if is_audit else [sys.executable, "-c", "print('ok')"],
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

            class _ExhaustionClient:
                def __init__(self) -> None:
                    self.verify_round = 0
                    self.saw_worker_trunc_warning = False
                    self.fix_prompts: List[str] = []
                    self.fix_done = False
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_texts = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                    def _reply(content: Optional[str], calls: Optional[List[Dict[str, Any]]] = None, finish_reason: str = "stop"):
                        m: Dict[str, Any] = {"role": "assistant", "content": content}
                        if calls:
                            m["tool_calls"] = calls
                        return serve._DryRunObject({"choices": [{"finish_reason": finish_reason, "message": m}]})

                    if sys_msg.startswith("You plan an engineering task"):
                        return _reply("Plan: validate schedule.csv.")

                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return _reply(json.dumps({"source": "llm_extractor", "deliverables": ["schedule.csv"]}))

                    if sys_msg.startswith("You check an engineer's work"):
                        if not tool_msgs and len(user_texts) == 1:
                            self.verify_round += 1
                        last_u = user_texts[-1]
                        if self.verify_round == 1:
                            if "engineer hit the step turn/time cap" in user_texts[0]:
                                self.saw_worker_trunc_warning = True
                            # Force MAX_NUDGES length truncation on final chunk with "PASS" text
                            if "reply was cut off" in last_u.lower() and len(user_texts) >= 4:
                                return _reply("PASS", finish_reason="length")
                            return _reply("Partial check...", finish_reason="length")
                        return _reply("PASS")

                    in_fix = any("A reviewer checked your files" in t for t in user_texts)
                    if in_fix:
                        gap_msg = next((t for t in reversed(user_texts) if "A reviewer checked your files" in t), "")
                        self.fix_prompts.append(gap_msg)
                        if not self.fix_done:
                            self.fix_done = True
                            return _reply(
                                None,
                                [
                                    {
                                        "id": "fix_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps({"path": "schedule.csv", "content": fixed_csv}),
                                        },
                                    }
                                ],
                            )
                        return _reply("Repaired schedule.csv cleanly.")

                    # WORKER runs tool calls until forced wrap_up (MAX_WORK_TURNS=3 -> truncated!)
                    last_u = user_texts[-1] if user_texts else ""
                    if serve.WRAP_UP["content"] in last_u:
                        return _reply("Worker stopped at turn cap.")
                    return _reply(
                        None,
                        [
                            {
                                "id": f"w_call_{len(tool_msgs)}",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": json.dumps({"path": "schedule.csv"})},
                            }
                        ],
                    )

            async def _run() -> str:
                with patch.dict(
                    os.environ,
                    {
                        "MODEL_NAME": "gemini-4-argon",
                        "OPENAI_BASE_URL": "http://proxy.local/v1",
                        "OPENAI_API_KEY": "k",
                        "MAX_FIX_ROUNDS": "2",
                        "MAX_WORK_TURNS": "3",
                        "MAX_VERIFY_TURNS": "10",
                        "MAX_FIX_TURNS": "5",
                    },
                    clear=False,
                ):
                    return await serve.solve(
                        sess,  # type: ignore[arg-type]
                        client=client,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )

            sess = _StressSession()
            client = _ExhaustionClient()
            ans = asyncio.run(_run())
            self.assertEqual(ans, "Repaired schedule.csv cleanly.")
            self.assertTrue(client.saw_worker_trunc_warning)
            self.assertEqual(client.verify_round, 2)
            self.assertTrue(any("worker: truncated=True" in entry for entry in sess.logs))
            self.assertTrue(any("verifier: truncated=True audit_errors=2" in entry for entry in sess.logs))
            self.assertTrue(client.fix_prompts)
            self.assertIn("missing/placeholder IDs", client.fix_prompts[0])
            self.assertIn("unparsed unit string '10 mm'", client.fix_prompts[0])
            self.assertIn("Verifier ran out of turns/time", client.fix_prompts[0])
            self.assertEqual((workdir / "schedule.csv").read_text(encoding="utf-8"), fixed_csv)

    def test_429_5xx_backoff_fallback_rotation_and_malformed_tool_calls_resilience(self):
        # Part A: HarnessExecutionEngine 429 + Retry-After + 5xx exponential backoff & rotation
        def _mock_http_response(status_code: int, headers: Optional[Dict[str, str]] = None) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=status_code,
                headers=dict(headers or {}),
                request=SimpleNamespace(method="POST", url="https://api.example/v1/chat/completions"),
            )

        sleep_calls: List[float] = []
        engine = HarnessExecutionEngine(
            config_path=str(REPO_ROOT / "submission.yaml"),
            max_retries=4,
            retry_backoff_sec=0.5,
            sleep_fn=sleep_calls.append,
        )

        class _RateLimitErr(Exception):
            status_code = 429
            retry_after = 2.0

        class _Overloaded503Err(Exception):
            status_code = 503
            response = _mock_http_response(503, headers={"Retry-After": "1"})

        class _Internal500Err(Exception):
            status_code = 500

        seen_models: List[str] = []

        def flaky_gen(_prompt: str, model_name: str, _max_tokens: int) -> str:
            seen_models.append(model_name)
            idx = len(seen_models)
            if idx == 1:
                raise _RateLimitErr("HTTP 429 RESOURCE_EXHAUSTED")
            if idx == 2:
                raise _Overloaded503Err("HTTP 503 overloaded")
            if idx == 3:
                raise _Internal500Err("HTTP 500 internal server error")
            return "recovered on fallback"

        res = engine.run_with_verification(
            prompt="Run model call.",
            generate_fn=flaky_gen,
            verify_fn=lambda out: (out == "recovered on fallback", "ok"),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(seen_models, ["gemini-4-argon", "gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.1-pro-preview"])
        self.assertEqual(sleep_calls, [2.0, 1.0, 2.0])
        self.assertEqual([h.get("error_kind") for h in res["history"][:3]], ["rate_limit", "server_error", "server_error"])
        kind, ra = classify_transient_error(_Overloaded503Err("503"))
        self.assertEqual((kind, ra), ("server_error", 1.0))

        # Part B: serve.agent + serve.recover on 429 Retry-After, 503, empty choices, and malformed tool_calls
        class _AgentSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}

            def __init__(self) -> None:
                self.logs: List[str] = []
                self.final_message = ""
                self.cancelled = asyncio.Event()

            def time_left(self) -> float:
                return 3600.0

            async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
                return {"exit_code": 0, "output": "ok", "truncated": False}

            async def read_file(self, path: str) -> bytes:
                raise taskbridge.TaskError(f"missing {path}")

            async def write_file(self, path: str, data: Any, append: bool = False) -> int:
                return len(str(data))

            async def log(self, text: str) -> None:
                self.logs.append(text)

            async def usage(self, model: str, inp: int, out: int, cached: int = 0) -> None:
                pass

        class _ChaosClient:
            def __init__(self) -> None:
                self.calls = 0
                self.seen_messages: List[List[Dict[str, Any]]] = []
                self.chat = self
                self.completions = self

            async def create(self, **kwargs: Any) -> Any:
                self.calls += 1
                self.seen_messages.append(json.loads(json.dumps(kwargs["messages"], default=str)))
                if self.calls == 1:
                    r429 = _mock_http_response(429, headers={"Retry-After": "8"})
                    raise openai.APIStatusError("rate limited", response=r429, body=None)
                if self.calls == 2:
                    r503 = _mock_http_response(503)
                    raise openai.APIStatusError("unavailable", response=r503, body=None)
                if self.calls == 3:
                    return serve._DryRunObject({"choices": []})
                if self.calls == 4:
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "finish_reason": "tool_calls",
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            None,
                                            {"id": "", "function": {"name": "read_file", "arguments": "{bad json"}},
                                            {"function": {"name": "bash", "arguments": '{"command": NaN}'}},
                                        ],
                                    },
                                }
                            ]
                        }
                    )
                return serve._DryRunObject(
                    {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Recovered cleanly."}}]}
                )

        async_sleeps: List[float] = []

        async def _fake_sleep(sec: float) -> None:
            async_sleeps.append(sec)

        async def _run_chaos() -> str:
            sess = _AgentSession()
            client = _ChaosClient()
            with patch.object(serve.asyncio, "sleep", side_effect=_fake_sleep):
                ans = await serve.agent(
                    sess,  # type: ignore[arg-type]
                    serve.conversation("Work in {workdir}", sess, "Recover."),  # type: ignore[arg-type]
                    serve.TOOLS,
                    60.0,
                    client=client,
                    runtime_cfg={
                        "model": "gemini-4-argon",
                        "max_output_tokens": 1024,
                        "effort": "high",
                        "cache_mark": False,
                        "max_failures": 3,
                    },
                    max_turns=10,
                )
            self.assertEqual(async_sleeps, [8.0, 15.0])
            self.assertTrue(any("empty choices" in entry for entry in sess.logs))
            # Check final request sent to model has 1-to-1 valid string tool_call_id matching normalized turn["tool_calls"]
            last_msgs = client.seen_messages[-1]
            asst_tc = next(m for m in last_msgs if m.get("role") == "assistant" and m.get("tool_calls"))
            tool_resps = [m for m in last_msgs if m.get("role") == "tool"]
            self.assertEqual(len(asst_tc["tool_calls"]), 3)
            self.assertEqual([c["id"] for c in asst_tc["tool_calls"]], [m["tool_call_id"] for m in tool_resps])
            self.assertTrue(all(isinstance(cid, str) and cid for cid in [m["tool_call_id"] for m in tool_resps]))
            self.assertTrue(all(m["content"].startswith("error:") for m in tool_resps))
            return ans

        self.assertEqual(asyncio.run(_run_chaos()), "Recovered cleanly.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
