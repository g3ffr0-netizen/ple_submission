"""
Comprehensive Stress & Edge-Case Test Suite for ple_submission.

Covers 5 stress areas:
  1. Circuit Breaker & Lockout
  2. Fallback Rotation & Retry Feedback
  3. Hostile Output Parsing (extract_json_payload & make_verify_fn)
  4. WebSocket Concurrency & Fuzzing (serve.py)
  5. Config & Eval Resilience (harness.py & eval/run.py --dry-run on 25 mixed tasks)
"""

from __future__ import annotations

import asyncio
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

from eval.run import build_task_verifier, evaluate_tasks, make_dry_run_generate_fn
from harness import (
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    HarnessExecutionEngine,
    extract_json_payload,
    load_submission_config,
    make_verify_fn,
)
from serve import handle_ws_message


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
        # Exactly 3 failing runs * 2 retries = 6 calls; remaining 17 runs blocked before generation
        self.assertEqual(gen_calls, 6)
        self.assertEqual(verify_calls, 6)

    def test_success_before_lockout_resets_failure_streak_and_recovery_after_reset(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=1,
            max_failures=3,
        )
        # 2 consecutive failures -> not locked yet
        for _ in range(2):
            r = engine.run_with_verification("t", lambda *_: "bad", lambda _: (False, "no"))
            self.assertFalse(r["ok"])
            self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 2)

        # 1 success -> streak resets to 0
        ok_r = engine.run_with_verification("t", lambda *_: "good", lambda _: (True, "ok"))
        self.assertTrue(ok_r["ok"])
        self.assertEqual(engine.failure_count, 0)
        self.assertFalse(engine.locked)

        # 3 more failures -> locks; reset -> recovers -> subsequent call works cleanly
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
            "gemini-4-hydrogen",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
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

        # Verify every retry prompt received the specific prior failure reason
        self.assertIn("upstream deadline exceeded", seen_prompts[1])
        self.assertIn("HTTP 503 overloaded", seen_prompts[2])
        self.assertIn("Output is empty", seen_prompts[3])
        self.assertIn("Output is empty", seen_prompts[4])
        self.assertIn("microgrid verified", seen_prompts[5].lower())
        # Ensure retry prompt does not compound endlessly
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
        """app.py and eval/run.py read result['elapsed_ms']; verify engine populates it."""
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
# 4. WebSocket Concurrency & Fuzzing (serve.py)
# ---------------------------------------------------------------------------
class TestStressWebSocketServe(unittest.TestCase):
    def test_60_concurrent_fuzzed_messages_never_crash(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=1, max_failures=5
        )

        async def mixed_runner(payload: Dict[str, Any]) -> Any:
            await asyncio.sleep(0.002)
            prompt = payload.get("prompt")
            if prompt == "explode":
                raise RuntimeError("runner exploded")
            if prompt == "bad_type":
                return "not-a-dict"
            if not isinstance(prompt, str) or not prompt.strip():
                return {"ok": False, "status": "invalid_prompt", "error": "Empty prompt."}
            return {
                "ok": True,
                "status": "verified",
                "output": f"ok:{prompt}",
                "model": engine.model,
            }

        messages: List[Any] = [
            '{"action": "ping", "request_id": "p1"}',
            '{"type": "ping", "request_id": "p2"}',
            '{"action": "status", "request_id": "s1"}',
            '{"type": "status", "request_id": "s2"}',
            '{"action": "execute", "prompt": "hello", "request_id": "e1"}',
            '{"type": "execute", "prompt": "hello_alias", "request_id": "e2"}',
            '{"action": "execute", "prompt": "explode", "request_id": "e3"}',
            '{"action": "execute", "prompt": "bad_type", "request_id": "e4"}',
            '{"action": "execute", "request_id": "e5_missing_prompt"}',
            '{"action": "reset", "request_id": "r1"}',
            '{"action": "unknown_op", "request_id": "u1"}',
            '{"action": "", "request_id": "u2"}',
            '{"action": null, "request_id": "u3"}',
            '{"action": 12345, "request_id": "u4"}',
            "{}",
            "{not json",
            "",
            "null",
            "[1, 2, 3]",
            '"bare string"',
            None,
            42,
            b'{"action": "ping"}',
        ]
        batch = messages * 3  # 69 concurrent requests

        async def _run_batch():
            return await asyncio.gather(
                *(handle_ws_message(msg, engine, mixed_runner) for msg in batch)
            )

        raw_responses = asyncio.run(_run_batch())
        self.assertEqual(len(raw_responses), len(batch))
        for raw in raw_responses:
            parsed = json.loads(raw)
            self.assertIsInstance(parsed, dict)
            self.assertIn("type", parsed)
            self.assertIn(
                parsed["type"], {"pong", "status", "reset", "result", "error", "lockout"}
            )

    def test_concurrent_failing_executes_enforce_lockout_without_race_overshoot(self):
        """
        Fire 20 concurrent async execute requests that always fail against an engine
        with max_failures=3. A lock-safe handler must not allow failure_count to
        overshoot max_failures due to TOCTOU races across await points.
        """
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml", max_retries=1, max_failures=3
        )
        runner_invocations = 0

        async def slow_failing_runner(payload: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal runner_invocations
            runner_invocations += 1
            await asyncio.sleep(0.01)
            return engine.run_with_verification(
                payload.get("prompt", "fail"),
                lambda *_: "bad",
                lambda _: (False, "fail"),
            )

        async def _fire_20():
            return await asyncio.gather(
                *(
                    handle_ws_message(
                        json.dumps(
                            {
                                "action": "execute",
                                "prompt": f"job_{i}",
                                "request_id": i,
                            }
                        ),
                        engine,
                        slow_failing_runner,
                    )
                    for i in range(20)
                )
            )

        responses = [json.loads(r) for r in asyncio.run(_fire_20())]
        self.assertTrue(engine.locked)
        self.assertEqual(engine.failure_count, 3)
        self.assertEqual(
            runner_invocations,
            3,
            f"Concurrency TOCTOU race: runner invoked {runner_invocations} times despite max_failures=3",
        )
        lockouts = [r for r in responses if r.get("type") == "lockout"]
        self.assertGreaterEqual(len(lockouts), 18)


# ---------------------------------------------------------------------------
# 5. Config & Eval Resilience
# ---------------------------------------------------------------------------
class TestStressConfigAndEval(unittest.TestCase):
    def test_malformed_and_hostile_submission_yaml_configs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # 1. Nonexistent path
            self.assertEqual(load_submission_config(str(tmp / "does_not_exist.yaml")), {})

            # 2. Directory path instead of file
            self.assertEqual(load_submission_config(str(tmp)), {})

            # 3. Empty file
            empty_f = tmp / "empty.yaml"
            empty_f.write_text("", encoding="utf-8")
            e_empty = HarnessExecutionEngine(config_path=str(empty_f))
            self.assertEqual(e_empty.model, DEFAULT_MODEL)
            self.assertEqual(e_empty.fallback_models, DEFAULT_FALLBACK_MODELS)

            # 4. Malformed YAML syntax
            bad_yaml = tmp / "bad.yaml"
            bad_yaml.write_text(
                "model: [unclosed\n  harness: : :\n\t::invalid", encoding="utf-8"
            )
            e_bad = HarnessExecutionEngine(config_path=str(bad_yaml))
            self.assertEqual(e_bad.model, DEFAULT_MODEL)

            # 5. Valid YAML but non-dict root (list)
            list_yaml = tmp / "list.yaml"
            list_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
            self.assertEqual(load_submission_config(str(list_yaml)), {})

            # 6. Hostile field types (JSON is valid YAML too)
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
        # 15 passing tasks
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
        # 10 failing tasks
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
        # Interleave: 4 fail -> 8 pass -> 6 fail -> 7 pass (25 tasks total)
        mixed_tasks: List[Dict[str, Any]] = []
        mixed_tasks.extend(tasks[15:19])
        mixed_tasks.extend(tasks[0:8])
        mixed_tasks.extend(tasks[19:25])
        mixed_tasks.extend(tasks[8:15])
        self.assertEqual(len(mixed_tasks), 25)

        # Part A: CLI end-to-end subprocess check with auto-reset on lockout
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

        # Part B: --no-reset-on-lockout keeps harness locked for remaining 22 tasks
        # Expects all 23 locked outcomes (task #3 locks + tasks #4..#25 blocked) to report locked=True
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
