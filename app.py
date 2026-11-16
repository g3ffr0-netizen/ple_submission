# =====================================================================
# VERIFICATION & EXECUTION HARNESS (harness.py)
# Enforces safety gates, tool retries, hallucination blocking, and lockout resets.
# =====================================================================

class HarnessExecutionEngine:
    def __init__(self, max_steps: int = 15, max_failures: int = 3):
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def generate_artifacts(self, task_prompt: str, result_payload: dict):
        """Log and store execution artifacts for complete auditability."""
        self.history.append({
            "prompt": task_prompt,
            "outcome": result_payload
        })

    def run_harness(self, task_prompt: str) -> dict:
        """Main execution loop entry point with enhanced input sanitization, 
        verification gates, and automatic reset on success."""
        try:
            # 1. Enhanced Input Sanitization & Type Enforcement
            if task_prompt is None:
                raise ValueError("INVALID_INPUT: task_prompt cannot be None.")
            if not isinstance(task_prompt, str):
                # Convert non-string inputs safely or raise an explicit error
                task_prompt = str(task_prompt)
            
            sanitized_prompt = task_prompt.strip()
            if len(sanitized_prompt) == 0:
                raise ValueError("EMPTY_PROMPT: Task prompt cannot be blank or whitespace-only.")

            # 2. Enforce Verification Thresholds (THE SAFETY LOCKOUT GATE)
            if self.failed_attempts >= self.max_failures:
                fail_res = {
                    "status": "FAILED", 
                    "reason": f"ENGINEERING_SAFETY_VIOLATION: Max failure threshold ({self.max_failures}) reached. Please reset harness state.",
                    "requires_reset": True
                }
                self.generate_artifacts(sanitized_prompt, fail_res)
                return fail_res

            # 3. Execution Simulation / Tool Routing
            if "fail" in sanitized_prompt.lower():
                raise RuntimeError("SIMULATED_TOOL_FAILURE: External tool returned a 500 error.")

            # 4. SUCCESS PATH & STATE RESET
            self.failed_attempts = 0 
            self.verification_passed = True
            
            final_output = {
                "status": "SUCCESS",
                "answer": f"Successfully executed and verified: {sanitized_prompt}",
                "history_length": len(self.history)
            }
            self.generate_artifacts(sanitized_prompt, final_output)
            return final_output

        except Exception as e:
            # FAILURE PATH & INCREMENT RETRY COUNTER
            self.verification_passed = False
            self.failed_attempts += 1
            
            fail_res = {
                "status": "FAILED",
                "reason": str(e),
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures
            }
            safe_prompt = str(task_prompt) if task_prompt else "UNKNOWN_PROMPT"
            self.generate_artifacts(safe_prompt, fail_res)
            return fail_res

    def reset_harness(self):
        """Manually resets failure counters and verification state after a lockout."""
        self.failed_attempts = 0
        self.verification_passed = False
        return {"status": "RESET", "message": "Harness state reset successfully."}
