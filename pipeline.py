"""Offline contract & regression test suite for HarnessExecutionEngine and PLE Protocol v1 serve.py."""

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from harness import (
    DEFAULT_MODEL,
    HarnessExecutionEngine,
    is_pass_verdict,
    load_submission_config,
    normalize_model_name,
)
import serve
import taskbridge


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
                "gemini-4-argon",
                "gemini-3.1-pro-preview",
            ],
        )

    def test_initial_state_and_normalization(self):
        self.assertEqual(self.engine.model, "gemini-3.8-flash")
        self.assertEqual(
            self.engine.fallback_models,
            ["gemini-4-argon", "gemini-3.1-pro-preview"],
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
            ["gemini-3.8-flash", "gemini-4-argon", "gemini-3.1-pro-preview"],
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


class ServeProtocolV1ContractTests(unittest.TestCase):
    def test_submission_yaml_ple_schema_contract(self):
        cfg_path = Path(__file__).resolve().parent / "submission.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "ple-submission")
        self.assertEqual(data["model_api"], "google-cloud/gemini-3.8-flash")
        self.assertTrue(
            set(data.keys()).issubset({"name", "model_api", "model_base_url", "install", "serve", "env"})
        )

    def test_solve_with_dry_run_client_and_session(self):
        class _FakeSession:
            instruction = "Write /app/hello.txt with HELLO"
            workdir = "/app"
            mcp_servers = []
            limits = {"agent_timeout_sec": 300}
            final_message = ""
            cancelled = asyncio.Event()

            def __init__(self):
                self.files = {}
                self.logs = []
                self.usages = []

            def time_left(self):
                return 300.0

            async def exec(self, command, timeout_sec=120):
                return {"exit_code": 0, "output": "/app\n", "truncated": False}

            async def read_file(self, path):
                if path not in self.files:
                    raise taskbridge.TaskError(f"missing {path}")
                return self.files[path]

            async def write_file(self, path, data, append=False):
                raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                self.files[path] = raw
                return len(raw)

            async def log(self, text):
                self.logs.append(text)

            async def usage(self, model, inp, out, cached=0):
                self.usages.append((model, inp, out, cached))

        async def _run():
            sess = _FakeSession()
            answer = await serve.solve(sess, dry_run=True)  # type: ignore[arg-type]
            self.assertTrue(answer.strip())
            self.assertEqual(sess.files.get("/app/hello.txt"), b"HELLO\n")
            self.assertGreaterEqual(len(sess.usages), 3)

        asyncio.run(_run())

    def test_run_tool_argument_sanitization_and_verdict_parser(self):
        class _MinSession:
            workdir = "/app"
            limits = {}

        async def _run():
            fn = {"name": "bash", "arguments": '{"command": "echo ok"}'}
            # Only verify error handling without live exec mock here: invalid JSON
            bad_fn = {"name": "bash", "arguments": "{not json"}
            err = await serve.run_tool(_MinSession(), bad_fn)  # type: ignore[arg-type]
            self.assertTrue(err.startswith("error:"))
            self.assertEqual(bad_fn["arguments"], "{}")
            # NaN must be rejected and sanitized
            nan_fn = {"name": "bash", "arguments": '{"command": NaN}'}
            nan_err = await serve.run_tool(_MinSession(), nan_fn)  # type: ignore[arg-type]
            self.assertTrue(nan_err.startswith("error:"))
            self.assertEqual(nan_fn["arguments"], "{}")
            return fn

        asyncio.run(_run())
        self.assertTrue(is_pass_verdict("PASS"))
        self.assertTrue(is_pass_verdict("**pass.**"))
        self.assertTrue(is_pass_verdict('{"verdict": "PASS"}'))
        self.assertFalse(is_pass_verdict("1. Output file is missing."))
        self.assertFalse(is_pass_verdict(""))

    def test_resolve_runtime_config_uses_evaluator_env_vars(self):
        with patch.dict(
            os.environ,
            {
                "MODEL_NAME": "gemini-4-argon",
                "OPENAI_BASE_URL": "http://127.0.0.1:4000/v1",
                "OPENAI_API_KEY": "proxy-key",
            },
            clear=False,
        ):
            cfg = serve.resolve_runtime_config()
            self.assertEqual(cfg["model"], "gemini-4-argon")
            self.assertEqual(cfg["base_url"], "http://127.0.0.1:4000/v1")
            self.assertEqual(cfg["api_key"], "proxy-key")


if __name__ == "__main__":
    unittest.main()
