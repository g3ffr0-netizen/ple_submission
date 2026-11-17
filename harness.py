from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-2.0-flash"):
        self.client = genai.Client()
        self.model_name = model_name

    @retry(
        # Retry only on GenAI API errors (which covers 429 rate limits and 5xx server errors)
        retry=retry_if_exception_type(APIError),
        # Wait exponentially: 2s -> 4s -> 8s (up to 3 attempts total)
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True # Reraise the final exception if all retries fail
    )
    def run_inference(
        self, 
        prompt: str, 
        system_instruction: str | None = None, 
        temperature: float = 0.7
    ) -> str:
        """
        Runs inference with automatic exponential backoff for rate limits.
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
        )

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config,
        )
        return response.text or ""
