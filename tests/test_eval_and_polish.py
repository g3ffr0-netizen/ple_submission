"""Focused unit tests covering polished behavior in app.py and eval/run.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from test_app import load_app
except ImportError:
    from tests.test_app import load_app

from eval.run import (
    build_task_verifier,
    evaluate_tasks,
    load_dev_tasks,
    make_dry_run_generate_fn,
)


class TestAppPolish(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_get_gemini_api_key_handles_none_secret_cleanly(self):
        with patch.object(self.app, "st") as mock_st, patch.dict(
            os.environ, {"GEMINI_API_KEY": "env-key-123"}, clear=False
        ):
            mock_st.secrets = {"GEMINI_API_KEY": None}
            self.assertEqual(self.app.get_gemini_api_key(), "env-key-123")

    def test_render_result_shows_primary_fallback_elapsed_and_breaker_state(self):
        engine = SimpleNamespace(
            model="gemini-4-hydrogen",
            failure_count=0,
            max_failures=3,
            locked=False,
        )

        # Primary success
        with patch.object(self.app, "st") as mock_st:
            self.app._render_result(
                {
                    "ok": True,
                    "status": "verified",
                    "attempt": 1,
                    "model": "gemini-4-hydrogen",
                    "output": "All good",
                    "elapsed_ms": 12.34,
                    "history": [{"attempt": 1, "model": "gemini-4-hydrogen", "ok": True}],
                },
                engine,
            )
            success_msg = mock_st.success.call_args[0][0]
            caption_msg = mock_st.caption.call_args[0][0]
            self.assertIn("primary model", success_msg.lower())
            self.assertIn("12.3 ms", caption_msg)
            self.assertIn("READY (0/3)", caption_msg)

        # Fallback success
        with patch.object(self.app, "st") as mock_st:
            self.app._render_result(
                {
                    "ok": True,
                    "status": "verified",
                    "attempt": 2,
                    "model": "gemini-3.8-flash",
                    "output": "Fallback worked",
                    "elapsed_ms": 45.6,
                    "history": [],
                },
                engine,
            )
            success_msg = mock_st.success.call_args[0][0]
            caption_msg = mock_st.caption.call_args[0][0]
            self.assertIn("fallback model", success_msg.lower())
            self.assertIn("gemini-3.8-flash (fallback)", caption_msg)

        # Locked failure
        locked_engine = SimpleNamespace(
            model="gemini-4-hydrogen",
            failure_count=3,
            max_failures=3,
            locked=True,
        )
        with patch.object(self.app, "st") as mock_st:
            mock_st.expander.return_value = MagicMock(
                __enter__=lambda s: s, __exit__=lambda s, *a: False
            )
            self.app._render_result(
                {
                    "ok": False,
                    "status": "locked",
                    "error": "Circuit breaker locked.",
                    "output": "bad output",
                    "elapsed_ms": 8.0,
                    "history": [{"attempt": 1, "model": "gemini-4-hydrogen", "ok": False}],
                },
                locked_engine,
            )
            self.assertIn("LOCKED (3/3)", mock_st.caption.call_args[0][0])


class TestEvalPolish(unittest.TestCase):
    def test_evaluate_tasks_aggregates_model_usage_and_empty_list(self):
        empty_report = evaluate_tasks([], lambda t: {})
        self.assertEqual(empty_report["total_tasks"], 0)
        self.assertEqual(empty_report["pass_rate"], 0.0)
        self.assertEqual(empty_report["avg_elapsed_ms"], 0.0)
        self.assertEqual(empty_report["model_usage"], {})

        tasks = [{"id": "t1"}, {"id": "t2"}]
        outcomes = iter(
            [
                {
                    "ok": True,
                    "status": "verified",
                    "model": "gemini-3.8-flash",
                    "attempt": 2,
                    "elapsed_ms": 10.0,
                    "locked": False,
                    "history": [
                        {"attempt": 1, "model": "gemini-4-hydrogen", "ok": False},
                        {"attempt": 2, "model": "gemini-3.8-flash", "ok": True},
                    ],
                },
                {
                    "ok": False,
                    "status": "locked",
                    "model": "",
                    "elapsed_ms": 2.0,
                    "locked": True,
                    "error": "Locked",
                    "history": [
                        {"attempt": 1, "model": "gemini-4-hydrogen", "ok": False},
                    ],
                },
            ]
        )
        reset_calls = 0

        def _reset():
            nonlocal reset_calls
            reset_calls += 1

        report = evaluate_tasks(tasks, lambda _t: next(outcomes), reset_fn=_reset)
        self.assertEqual(report["total_tasks"], 2)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["lockouts"], 1)
        self.assertEqual(report["avg_elapsed_ms"], 6.0)
        self.assertEqual(reset_calls, 1)
        self.assertEqual(
            report["model_usage"],
            {
                "gemini-4-hydrogen": {"attempts": 2, "verified": 0, "failed_attempts": 2},
                "gemini-3.8-flash": {"attempts": 1, "verified": 1, "failed_attempts": 0},
            },
        )

    def test_build_task_verifier_and_dry_run_min_chars_resilience(self):
        task = {
            "prompt": "Return JSON",
            "min_chars": 80,
            "required_terms": ["microgrid"],
            "require_json": True,
        }
        verify = build_task_verifier(task)
        gen = make_dry_run_generate_fn(task)
        output = gen("Return JSON", "gemini-4-hydrogen", 4096)
        self.assertGreaterEqual(len(output), 80)
        self.assertTrue(verify(output)[0])
        self.assertFalse(verify(None)[0])  # type: ignore[arg-type]
        self.assertFalse(verify(123)[0])  # type: ignore[arg-type]

        with patch("eval.run.json.loads", side_effect=RecursionError("too deep")):
            ok, reason = verify('{"a": 1, "microgrid": "' + ("x" * 70) + '"}')
        self.assertFalse(ok)
        self.assertIn("depth", reason.lower())

    def test_load_dev_tasks_rejects_non_dict_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_json = Path(tmp_dir) / "bad.json"
            bad_json.write_text(json.dumps(["not-a-dict"]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_dev_tasks(bad_json)

            bad_jsonl = Path(tmp_dir) / "bad.jsonl"
            bad_jsonl.write_text('{"prompt": "ok"}\n123\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_dev_tasks(bad_jsonl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
