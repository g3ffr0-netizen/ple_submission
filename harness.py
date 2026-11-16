# =====================================================================
# STRESS TEST SUITE FOR HARNESS EXECUTION ENGINE (test_harness.py)
# =====================================================================
import unittest
from harness import HarnessExecutionEngine

class TestHarnessExecutionEngine(unittest.TestCase):
    
    def setUp(self):
        # Initialize with max_failures=2 for rapid threshold testing
        self.engine = HarnessExecutionEngine(max_steps=5, max_failures=2)

    def test_01_successful_execution(self):
        """Verify normal success path resets failure counter."""
        res = self.engine.run_harness("Process telemetry data normal flow")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(self.engine.verification_passed)
        self.assertEqual(self.engine.failed_attempts, 0)

    def test_02_simulated_tool_failure(self):
        """Verify tool failures increment failure counter correctly."""
        res = self.engine.run_harness("Trigger a fail condition")
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(res["failed_attempts"], 1)
        self.assertFalse(self.engine.verification_passed)

    def test_03_safety_lockout_gate(self):
        """Verify max failures triggers the hard engineering safety lockout gate."""
        # 1st failure
        self.engine.run_harness("fail step one")
        # 2nd failure (hits max_failures=2 threshold)
        self.engine.run_harness("fail step two")
        
        # 3rd attempt must trigger the lockout even with a valid prompt
        lockout_res = self.engine.run_harness("Valid execution prompt post-lockout")
        self.assertEqual(lockout_res["status"], "FAILED")
        self.assertTrue(lockout_res.get("requires_reset", False))
        self.assertIn("ENGINEERING_SAFETY_VIOLATION", lockout_res["reason"])

    def test_04_manual_reset(self):
        """Verify manual reset clears lockout state and resets counters."""
        self.engine.run_harness("fail step one")
        self.engine.run_harness("fail step two")
        
        # Execute reset
        reset_res = self.engine.reset_harness()
        self.assertEqual(reset_res["status"], "RESET")
        self.assertEqual(self.engine.failed_attempts, 0)
        self.assertFalse(self.engine.verification_passed)
        
        # Subsequent prompt should execute successfully
        success_res = self.engine.run_harness("Valid execution post-reset")
        self.assertEqual(success_res["status"], "SUCCESS")

    def test_05_edge_case_inputs(self):
        """Verify empty, whitespace, and None-type inputs are safely handled."""
        # Whitespace test
        res_ws = self.engine.run_harness("   ")
        self.assertEqual(res_ws["status"], "FAILED")
        self.assertIn("EMPTY_PROMPT", res_ws["reason"])

        # None type test
        res_none = self.engine.run_harness(None)
        self.assertEqual(res_none["status"], "FAILED")
        self.assertIn("INVALID_INPUT", res_none["reason"])

        # Integer coercion test
        res_int = self.engine.run_harness(9999)
        self.assertEqual(res_int["status"], "SUCCESS")

if __name__ == "__main__":
    unittest.main()
