# =====================================================================
# HARDENED HARNESS EXECUTION ENGINE (harness.py)
# =====================================================================
import os
from google import genai

class HarnessExecutionEngine:
    """
    Production-grade execution engine featuring safety lockout gates,
    flexible input coercion, audit trail tracking, and live Gemini API integration.
    """

    def __init__(self, max_steps: int = 15, max_failures: int = 3):
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def _log_artifact(self, prompt: str, result_payload: dict):
        """Internal helper to safely record execution state to history."""
        self.history.append({"prompt": prompt, "outcome": result_payload})

    def run_harness(self, task_prompt) -> dict:
        """
        Executes a harness step with safety lockout, strict input validation,
        and live Gemini model generation.
        """
        # 1. Check Safety Lockout Gate First
        if self.failed_attempts >= self.max_failures:
            self.verification_passed = False
            fail_res = {
                "status": "FAILED",
                "reason": "ENGINEERING_SAFETY_VIOLATION: Max failure threshold reached.",
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures,
                "requires_reset": True
            }
            self._log_artifact(str(task_prompt) if task_prompt else "LOCKED_OUT", fail_res)
            return fail_res

        # 2. Validate None or Empty Input with Coercion
        if task_prompt is None:
            self.verification_passed = False
            fail_res = {
                "status": "FAILED",
                "reason": "INVALID_INPUT: Task prompt cannot be None.",
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures
            }
            self._log_artifact("None", fail_res)
            return fail_res

        # Coerce non-strings (e.g. ints) to string, then strip whitespace
        sanitized_prompt = str(task_prompt).strip()
        if not sanitized_prompt:
            self.verification_passed = False
            self.failed_attempts += 1
            fail_res = {
                "status": "FAILED",
                "reason": "EMPTY_PROMPT: Task prompt cannot be blank or whitespace.",
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures,
                "requires_reset": self.failed_attempts >= self.max_failures
            }AQ.Ab8RN6L0LAT5aekdRO33PmCIznr23h9LKznEknzAxvCEo-nDag
            self._log_artifact(sanitized_prompt, fail_res)
            return fail_res

        try:
                       # 3. Environment & Client Verification
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("Missing GEMINI_API_KEY environment variable. Check Streamlit Secrets.")
                raise ValueError("Missing GEMINI_API_KEY. Did you paste it into line 64?")

            client = genai.Client(api_key=api_key)
            
            # 4. Live Gemini API Call
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=sanitized_prompt
            )
            
            # 5. Success Flow
            self.failed_attempts = 0
            self.verification_passed = True
            
            success_res = {
                "status": "SUCCESS",
                "answer": response.text,
                "failed_attempts": self.failed_attempts,
                "history_length": len(self.history) + 1
            }
            self._log_artifact(sanitized_prompt, success_res)
            return success_res

        except Exception as e:
            # 6. Failure & Exception Handling
            self.verification_passed = False
            self.failed_attempts += 1
            fail_res = {
                "status": "FAILED",
                "reason": str(e),
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures,
                "requires_reset": self.failed_attempts >= self.max_failures
            }
            self._log_artifact(sanitized_prompt, fail_res)
            return fail_res

    def reset_harness(self) -> dict:
        """Manually resets engine state, failure counter, and lockout gates."""
        self.failed_attempts = 0
        self.verification_passed = False
        reset_res = {
            "status": "RESET",
            "message": "Harness state reset successfully.",
            "failed_attempts": self.failed_attempts
        }
        self._log_artifact("SYSTEM_RESET", reset_res)
        return reset_res
