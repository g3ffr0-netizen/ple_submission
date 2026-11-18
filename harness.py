import os
from typing import Optional

from google import genai


class HarnessExecutionEngine:
    """Bounded execution engine for Gemini-backed task runs."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.0-flash",
        max_failures: int = 3,
        max_steps: int = 3,
    ):
        if not isinstance(max_failures, int) or isinstance(max_failures, bool) or max_failures < 1:
            raise ValueError("max_failures must be a positive integer")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")

        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise ValueError("Gemini API key is missing. Pass api_key or set GEMINI_API_KEY.")

        self.model_name = model_name
        self.max_failures = max_failures
        self.max_steps = max_steps
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []
        self.client = genai.Client(api_key=resolved_key.strip(), vertexai=False)

    def reset_harness(self) -> None:
        """Return the engine to its initial unlocked state."""
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def run_harness(self, task: str, api_key: Optional[str] = None) -> dict:
        """Run a task subject to retry, verification, and lockout gates."""
        del api_key

        if self.failed_attempts >= self.max_failures:
            return {
                "status": "LOCKED",
                "requires_reset": True,
                "reason": "Maximum failure count reached. Reset the engine to continue.",
                "failed_attempts": self.failed_attempts,
            }

        if not isinstance(task, str) or not task.strip():
            return {
                "status": "FAILED",
                "requires_reset": False,
                "reason": "Task prompt must be a non-empty string.",
                "failed_attempts": self.failed_attempts,
            }

        prompt = (
            "Answer the user's task directly and accurately. "
            "Do not claim to perform actions you cannot perform.\n\n"
            f"Task: {task.strip()}"
        )

        for step in range(1, self.max_steps + 1):
            if self.failed_attempts >= self.max_failures:
                break

            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                answer = getattr(response, "text", None)

                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("The model returned an empty response")

                answer = answer.strip()
                self.verification_passed = True
                self.history.append(
                    {"step": step, "status": "SUCCESS", "task": task.strip(), "answer": answer}
                )
                return {
                    "status": "SUCCESS",
                    "requires_reset": False,
                    "answer": answer,
                    "failed_attempts": self.failed_attempts,
                }

            except Exception as exc:
                self.failed_attempts += 1
                reason = str(exc).strip() or exc.__class__.__name__
                self.verification_passed = False
                self.history.append(
                    {"step": step, "status": "FAILED", "task": task.strip(), "reason": reason}
                )

        locked = self.failed_attempts >= self.max_failures
        return {
            "status": "LOCKED" if locked else "FAILED",
            "requires_reset": locked,
            "reason": (
                "Maximum failure count reached. Reset the engine to continue."
                if locked
                else "Verification retries exhausted."
            ),
            "failed_attempts": self.failed_attempts,
        }
