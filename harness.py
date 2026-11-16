import os
from google import genai

class HarnessExecutionEngine:
    def __init__(self, max_steps: int = 15, max_failures: int = 3):
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def _log_artifact(self, prompt: str, result_payload: dict):
        self.history.append({"prompt": prompt, "outcome": result_payload})

    def run_harness(self, task_prompt) -> dict:
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
            }
            self._log_artifact(sanitized_prompt, fail_res)
            return fail_res

        try:
            # --- PASTE YOUR ACTUAL KEY BETWEEN THE QUOTES ON THE NEXT LINE ---
            api_key = "PASTE_YOUR_KEY_HERE"
            
            if not api_key or api_key == "PASTE_YOUR_KEY_HERE":
                raise ValueError("Missing key. Paste it into harness.py.")

            client = genai.Client(api_key=api_key)
            
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=sanitized_prompt
            )
            
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
        self.failed_attempts = 0
        self.verification_passed = False
        reset_res = {
            "status": "RESET",
            "message": "Harness state reset successfully.",
            "failed_attempts": self.failed_attempts
        }
        self._log_artifact("SYSTEM_RESET", reset_res)
        return reset_res
