import os
import streamlit as st
from typing import Optional
from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from pydantic import BaseModel, Field

# --- 1. Define the expected output schema ---
class HarnessEvaluationResult(BaseModel):
    is_successful: bool = Field(
        description="Whether the task prompt passed evaluation."
    )
    reasoning: str = Field(
        description="Step-by-step reasoning explaining the result."
    )
    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0."
    )

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-2.0-flash"):
        self.model_name = model_name
        self._test_failure_count = 0

        # 2. Resolve API Key (Secrets -> Environment)
        api_key = None
        try:
            if hasattr(st, "secrets") and "GEMINI_API_KEY" in st.secrets:
                api_key = st.secrets["GEMINI_API_KEY"]
        except Exception:
            pass
            
        if not api_key:
            api_key = os.environ.get("GEMINI_API_KEY")

        if not api_key:
            raise ValueError("Missing API Key. Set GEMINI_API_KEY in Streamlit Secrets.")

        # 3. FORCE Developer API (vertexai=False) to prevent the Vertex AI crash
        try:
            self.client = genai.Client(api_key=api_key, vertexai=False)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize GenAI Client: {str(e)}")

    def run_structured_inference(
        self, 
        prompt: str, 
        status_callback=None,
        system_instruction: Optional[str] = None, 
        temperature: float = 0.2,
        simulate_errors: bool = False
    ) -> HarnessEvaluationResult:
        
        # 4. Configure the generation settings
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=HarnessEvaluationResult,
        )

        @retry(
            retry=retry_if_exception_type(APIError),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            stop=stop_after_attempt(3),
            before_sleep=lambda retry_state: self._log_retry(retry_state, status_callback),
            reraise=True
        )
        def _execute():
            # Simulate errors for testing retry logic
            if simulate_errors and self._test_failure_count < 2:
                self._test_failure_count += 1
                raise APIError(429, {"message": "Simulated rate limit."})
            
            self._test_failure_count = 0

            # Execute the API call
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            
            # Return the parsed Pydantic object
            if response.parsed:
                return response.parsed
            else:
                raise ValueError("Model returned unparseable response.")

        return _execute()

    def _log_retry(self, retry_state, callback):
        attempt = retry_state.attempt_number
        exception = retry_state.outcome.exception()
        wait_time = getattr(retry_state.next_action, "sleep", 0) if retry_state.next_action else 0
        
        if callback:
            callback(attempt, str(exception), wait_time)
