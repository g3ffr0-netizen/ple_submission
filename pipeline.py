# pipeline.py
"""Offline contract checks for HarnessExecutionEngine."""

import unittest
from unittest.mock import patch

from harness import HarnessExecutionEngine


class HarnessContractTests(unittest.TestCase):
    def setUp(self):
        client_patcher = patch("harness.genai.Client")
        self.mock_client = client_patcher.start()
        self.addCleanup(client_patcher.stop)

        self.engine = HarnessExecutionEngine(
            api_key="test-key-not-for-real-use",
            max_failures=3,
        )

    def test_initial_state(self):
        self.assertEqual(self.engine.max_failures, 3)
        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)
        self.assertIsInstance(self.engine.history, list)
        self.assertGreater(self.engine.max_steps, 0)

    def test_required_methods_are_callable(self):
        for method_name in ("run_harness", "reset_harness"):
            with self.subTest(method=method_name):
                method = getattr(self.engine, method_name, None)
                self.assertTrue(callable(method))

    def test_reset_harness_restores_initial_state(self):
        self.engine.failed_attempts = 2
        self.engine.verification_passed = True
        self.engine.history.append({"unexpected": "entry"})

        self.engine.reset_harness()

        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)
        self.assertEqual(self.engine.history, [])

    def test_reset_harness_is_safe_after_initialization(self):
        self.engine.reset_harness()
        self.engine.reset_harness()

        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)

    def test_invalid_max_failures_values_are_rejected(self):
        for value in (-1, 0):
            with self.subTest(max_failures=value):
                with self.assertRaises(ValueError):
                    HarnessExecutionEngine(
                        api_key="test-key-not-for-real-use",
                        max_failures=value,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
