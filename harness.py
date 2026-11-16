import os
from google import genai

class HarnessExecutionEngine:
    def __init__(self, max_steps: int = 15, max_failures: int = 3):
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def generate_artifacts(self, task_prompt: str, result_payload: dict):
        self.history.append({"prompt": task_prompt, "outcome": result_payload})

    def run_harness(self, task_prompt: str) -> dict:
        try:
            # 1. Sanitization
            if not task_prompt or not isinstance(task_prompt, str):
                raise ValueError("INVALID_INPUT: task_prompt must be a non-empty string.")
            sanitized_prompt = task_prompt.strip()
            if len(sanitized_prompt) == 0:
                raise ValueError("EMPTY_PROMPT: Task prompt cannot be blank.")

            # 2. Lockout Gate
            if self.failed_attempts >= self.max_failures:
                return {"status": "FAILED", "reason": "ENGINEERING_SAFETY_VIOLATION: Max failures reached.", "requires_reset": True}

            # 3. THE ACTUAL GEMINI CALL
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("Missing GEMINI_API_KEY environment variable.")

            client = genai.Client(api_key=api_key)
            
            # Send the prompt to Gemini
            response = client.models.generate_content(
                model="gemini-2.5-flash", # Using flash for speed and cost
                contents=sanitized_prompt
            )
            
            # 4. Success
            self.failed_attempts = 0
            self.verification_passed = True
            
            final_output = {
                "status": "SUCCESS",
                "answer": response.text, # The actual AI response!
                "history_length": len(self.history)
            }
            self.generate_artifacts(sanitized_prompt, final_output)
            return final_output

        except Exception as e:
            self.verification_passed = False
            self.failed_attempts += 1
            fail_res = {
                "status": "FAILED",
                "reason": str(e),
                "failed_attempts": self.failed_attempts,
                "max_attempts": self.max_failures
            }
            self.generate_artifacts(task_prompt or "UNKNOWN", fail_res)
            return fail_res

    def reset_harness(self):
        self.failed_attempts = 0
        self.verification_passed = False
        return {"status": "RESET", "message": "Harness state reset successfully."}
