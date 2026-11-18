import os
from typing import Optional
from google import genai


class HarnessExecutionEngine:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.0-flash",
        max_failures: int = 3,
    ):
        if not isinstance(max_failures, int) or max_failures < 1:
            raise ValueError("max_failures must be a positive integer")

        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise ValueError(
                "Gemini API key is missing. Pass api_key or set GEMINI_API_KEY."
            )

        self.model_name = model_name
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self._test_failure_count = 0

        self.client = genai.Client(
            api_key=resolved_key.strip(),
            vertexai=False,
        )
