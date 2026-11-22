"""Offline contract & regression test suite for HarnessExecutionEngine and serve.py."""

import asyncio
import json
import unittest

from harness import (
    DEFAULT_MODEL,
    HarnessExecutionEngine,
    load_submission_config,
    normalize_model_name,
)
from serve import handle_ws_message


class HarnessContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = HarnessExecutionEngine(
            model="google-cloud/gemini-3.8-flash",
            config_path="nonexistent_submission.yaml",
            max_retries=3,
            max_failures=2,
            max_output_tokens=4096,
            fallback_models=[
                "gemini-3.8-flash",
                "gemini-3.5-flash",
                "gemini-3.1-pro",
            ],
        )

    def test_initial_state_and_normalization(self):
        self.assertEqual(self.engine.model, "gemini-3.8-flash")
        self.assertEqual(
            self.engine.fallback_models,
            ["gemini-3.5-flash", "gemini-3.1-pro"],
        )
        self.assertEqual(self.engine.max_retries, 3)
        self.assertEqual(self.engine.max_failures, 2)
        self.assertEqual(self.engine.max_output_tokens, 4096)
        self.assertEqual(self.engine.failure_count, 0)
        self.assertFalse(self.engine.locked)
        self.assertEqual(normalize_model_name(None), DEFAULT_MODEL)
        self.assertEqual(load_submission_config("nonexistent.yaml"), {})

    def test_required_methods_are_callable(self):
        for method_name in ("run_with_verification", "reset"):
            with self.subTest(method=method_name):
                self.assertTrue(callable(getattr(self.engine, method_name, None)))

    def test_safe_int_guards_clamp_invalid_and_boolean_values(self):
        guarded = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=True,
            max_failures=-5,
            max_output_tokens=10,
        )
        self.assertEqual(guarded.max_retries, 3)
        self.assertEqual(guarded.max_failures, 1)
        self.assertEqual(guarded.max_output_tokens, 64)

    def test_invalid_prompt_does_not_penalize_circuit_breaker(self):
        res = self.engine.run_with_verification(
            "   ",
            lambda p, m, t: "ignored",
            lambda o: (True, "ok"),
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid_prompt")
        self.assertEqual(self.engine.failure_count, 0)
        self.assertFalse(self.engine.locked)

    def test_retry_rotation_and_exception_recovery(self):
        models_called = []

        def flaky_generate(prompt, model, max_tokens):
            models_called.append(model)
            if len(models_called) == 1:
                raise RuntimeError("Transient 503")
            if len(models_called) == 2:
                return "draft_v1"
            return "verified_answer"

        def verify_fn(output):
            if output == "draft_v1":
                return False, "Missing final verification token"
            return output == "verified_answer", "All checks passed"

        res = self.engine.run_with_verification("Run task", flaky_generate, verify_fn)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "verified")
        self.assertEqual(res["attempt"], 3)
        self.assertEqual(
            models_called,
            ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-pro"],
        )
        self.assertEqual(len(res["history"]), 3)
        self.assertEqual(self.engine.failure_count, 0)

    def test_circuit_breaker_lock_and_reset(self):
        engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=1,
            max_failures=2,
        )
        r1 = engine.run_with_verification(
            "Task",
            lambda p, m, t: "bad",
            lambda o: (False, "wrong"),
        )
        self.assertEqual(r1["status"], "verification_retries_exhausted")
        self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 1)

        r2 = engine.run_with_verification(
            "Task",
            lambda p, m, t: "bad",
            lambda o: (False, "wrong"),
        )
        self.assertEqual(r2["status"], "locked")
        self.assertTrue(engine.locked)
        self.assertEqual(engine.failure_count, 2)

        # Locked engine must refuse execution until reset
        r3 = engine.run_with_verification(
            "Task",
            lambda p, m, t: "good",
            lambda o: (True, "ok"),
        )
        self.assertEqual(r3["status"], "locked")

        state = engine.reset()
        self.assertEqual(state["status"], "reset")
        self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 0)

        r4 = engine.run_with_verification(
            "Task",
            lambda p, m, t: "good",
            lambda o: (True, "ok"),
        )
        self.assertTrue(r4["ok"])


class ServeWebSocketContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = HarnessExecutionEngine(
            config_path="nonexistent.yaml",
            max_retries=1,
            max_failures=1,
        )

    def _send(self, raw_message, runner=None):
        async def _invoke():
            return await handle_ws_message(
                raw_message,
                self.engine,
                run_harness_fn=runner,
            )

        return json.loads(asyncio.run(_invoke()))

    def test_ping_and_status(self):
        pong = self._send('{"action": "ping"}')
        self.assertEqual(pong["type"], "pong")

        # Backward-compatible `type` alias
        pong_alias = self._send('{"type": "ping"}')
        self.assertEqual(pong_alias["type"], "pong")

        status = self._send('{"action": "status"}')
        self.assertEqual(status["type"], "status")
        self.assertFalse(status["locked"])
        self.assertEqual(status["failure_count"], 0)

    def test_invalid_json_returns_error(self):
        resp = self._send("{not valid json")
        self.assertEqual(resp["type"], "error")

    def test_execute_and_reset(self):
        def fake_runner(payload):
            prompt = payload.get("prompt", "") if isinstance(payload, dict) else str(payload)
            return {
                "ok": True,
                "status": "verified",
                "output": f"echo:{prompt}",
                "model": self.engine.model,
            }

        ok_resp = self._send(
            '{"action": "execute", "prompt": "hello"}',
            runner=fake_runner,
        )
        self.assertEqual(ok_resp["type"], "result")
        self.assertTrue(ok_resp["ok"])
        self.assertEqual(ok_resp["output"], "echo:hello")

        reset_resp = self._send('{"action": "reset"}')
        self.assertEqual(reset_resp["type"], "reset")
        self.assertFalse(reset_resp["locked"])


if __name__ == "__main__":
    unittest.main()
