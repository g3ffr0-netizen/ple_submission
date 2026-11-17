import time
from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-2.0-flash"):
        self.client = genai.Client()
        self.model_name = model_name

    def run_inference_with_status(
        self, 
        prompt: str, 
        status_callback=None,
        system_instruction: str | None = None, 
        temperature: float = 0.7
    ) -> str:
        """
        Runs inference with tenacity retries and reports status changes via callback.
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
        )

        # Define the retry behavior dynamically so we can inject the callback
        @retry(
            retry=retry_if_exception_type(APIError),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            stop=stop_after_attempt(3),
            before_sleep=lambda retry_state: self._log_retry(retry_state, status_callback),
            reraise=True
        )
        def _execute():
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            return response.text or ""

        return _execute()

    def _log_retry(self, retry_state, callback):
        """Helper triggered by tenacity before sleeping/retrying."""
        attempt = retry_state.attempt_number
        exception = retry_state.outcome.exception()
        wait_time = getattr(retry_state.next_action, "sleep", 0)
        
        if callback:
            callback(attempt, str(exception), wait_time)
