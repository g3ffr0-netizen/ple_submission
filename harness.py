import os
import time
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
        history_limit: int = 100,
    ):
        if not isinstance(max_failures, int) or isinstance(max_failures, bool) or max_failures < 1:
            raise ValueError("max_failures must be a positive integer")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if not isinstance(history_limit, int) or isinstance(history_limit, bool) or history_limit < 1:
            raise ValueError("history_limit must be a positive integer")

        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise ValueError("Gemini API key is missing. Pass api_key or set GEMINI_API_KEY.")

        self.model_name = model_name
        self.max_failures = max_failures
        self.max_steps = max_steps
        self.history_limit = history_limit
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []
        self.client = genai.Client(api_key=resolved_key.strip(), vertexai=False)

    def reset_harness(self) -> None:
        """Return the engine to its initial unlocked state."""
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []

    def _record_history(self, entry: dict) -> None:
        """Append a bounded history entry to avoid unbounded session memory use."""
        self.history.append(entry)
        if len(self.history) > self.history_limit:
            del self.history[:-self.history_limit]

    @staticmethod
    def _is_non_retryable(exc: Exception) -> bool:
        """Stop retries for deterministic configuration/client-side failures."""
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(exc, "code", None)

        if isinstance(status_code, int) and status_code in (400, 401, 403, 404):
            return True

        message = str(exc).lower()
        return any(marker in message for marker in (
            "api key",
            "permission denied",
            "unauthenticated",
            "invalid argument",
        ))

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

        clean_task = task.strip()
        prompt = (
            "Answer the user's task directly and accurately. "
            "Do not claim to perform actions you cannot perform.\n\n"
            f"Task: {clean_task}"
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
                self._record_history(
                    {"step": step, "status": "SUCCESS", "task": clean_task, "answer": answer}
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
                self._record_history(
                    {"step": step, "status": "FAILED", "task": clean_task, "reason": reason}
                )

                if self._is_non_retryable(exc):
                    break

                if step < self.max_steps and self.failed_attempts < self.max_failures:
                    time.sleep(min(0.25 * (2 ** (step - 1)), 2.0))

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
