# =====================================================================
# HARNESS EXECUTION ENGINE (harness.py)
# =====================================================================

class HarnessExecutionEngine:
    """
    Execution engine with built-in failure tracking, safety lockout gates,
    and input validation.
    """

    def __init__(self, max_steps: int = 5, max_failures: int = 2):
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False

    def run_harness(self, prompt) -> dict:
        """
        Executes a harness step with safety lockout and edge-case validation.
        """
        # 1. Check safety lockout gate first
        if self.failed_attempts >= self.max_failures:
            self.verification_passed = False
            return {
                "status": "FAILED",
                "reason": "ENGINEERING_SAFETY_VIOLATION: Max failure threshold reached.",
                "failed_attempts": self.failed_attempts,
                "requires_reset": True,
            }

        # 2. Validate None input
        if prompt is None:
            self.verification_passed = False
            return {
                "status": "FAILED",
                "reason": "INVALID_INPUT: Prompt cannot be None.",
                "failed_attempts": self.failed_attempts,
            }

        # 3. Coerce to string (handles integers/other types) and check whitespace
        prompt_str = str(prompt).strip()
        if not prompt_str:
            self.verification_passed = False
            return {
                "status": "FAILED",
                "reason": "EMPTY_PROMPT: Prompt cannot be empty or whitespace.",
                "failed_attempts": self.failed_attempts,
            }

        # 4. Simulated tool failure condition
        if "fail" in prompt_str.lower():
            self.failed_attempts += 1
            self.verification_passed = False
            return {
                "status": "FAILED",
                "reason": "SIMULATED_TOOL_FAILURE: Execution encountered a fail condition.",
                "failed_attempts": self.failed_attempts,
                "requires_reset": self.failed_attempts >= self.max_failures,
            }

        # 5. Normal success path resets failure counter
        self.failed_attempts = 0
        self.verification_passed = True
        return {
            "status": "SUCCESS",
            "answer": f"Executed: {prompt_str}",  # Aligned with app.py expectation
            "failed_attempts": self.failed_attempts,
        }

    def reset_harness(self, *args, **kwargs) -> dict:
        """
        Manually resets failure counters and clears safety lockout state.
        """
        self.failed_attempts = 0
        self.verification_passed = False
        return {
            "status": "RESET",
            "failed_attempts": self.failed_attempts,
        }
