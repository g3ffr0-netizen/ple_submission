# pipeline.py
"""Offline contract checks for HarnessExecutionEngine.

Run from the repository root:
    python pipeline.py
"""

import unittest
from unittest.mock import patch

from harness import HarnessExecutionEngine


class HarnessContractTests(unittest.TestCase):
    def setUp(self):
        # Prevent tests from creating a real Gemini client.
        client_patcher = patch("harness.genai.Client")
        self.mock_client = client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.engine = HarnessExecutionEngine(
            api_key="test-key-not-for-real-use",
            max_failures=3,
        )

    def test_engine_has_expected_state(self):
        self.assertEqual(self.engine.max_failures, 3)
        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)

    def test_engine_exposes_expected_methods(self):
        required_methods = ("run_harness", "reset_harness")
        for method in required_methods:
            with self.subTest(method=method):
                self.assertTrue(
                    callable(getattr(self.engine, method, None)),
                    f"HarnessExecutionEngine is missing {method}()",
                )

    def test_engine_exposes_history_and_step_limit(self):
        self.assertTrue(
            hasattr(self.engine, "history"),
            "HarnessExecutionEngine is missing history",
        )
        self.assertTrue(
            hasattr(self.engine, "max_steps"),
            "HarnessExecutionEngine is missing max_steps",
        )

    def test_reset_method_is_callable(self):
        reset = getattr(self.engine, "reset_harness", None)
        if not callable(reset):
            self.skipTest("reset_harness() is not implemented yet")

        reset()
        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
