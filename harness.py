import os
import streamlit as st
from typing import Optional
from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from pydantic import BaseModel, Field

class HarnessEvaluationResult(BaseModel):
    is_successful: bool = Field(description="Whether the task passed evaluation.")
    reasoning: str = Field(description="Step-by-step reasoning.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Confidence score.")

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-3.8-flash", max_steps: int = 15, max_failures: int = 3):
        self.model_name = model_name
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self.history = []
        self._test_failure_count = 0

        # Clear conflicting Cloud variables
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        os.environ.pop("GOOGLE_CLOUD_LOCATION", None)
        os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)

        # Resolve API Key
        api_key = None
        try:
            if hasattr(st, "secrets") and "GEMINI_API_KEY" in st.secrets:
                api_key = st.secrets["GEMINI_API_KEY"].strip()
        except Exception:
            pass
            
        if not api_key:
            api_key = os.environ.get("GEMINI_API_KEY")
            if api_key: api_key = api_key.strip()

        if not api_key:
            raise ValueError("Missing API Key. Set GEMINI_API_KEY in Streamlit Secrets.")

        # Initialize Client
        self.client = genai.Client(api_key=api_key, vertexai=False)

    def _log_artifact(self, prompt: str, result_payload: dict):
        self.history.append({"prompt": prompt, "outcome": result_payload})

    # BACKWARD COMPATIBLE METHOD FOR APP.PY
    def run_harness(self, task_prompt, api_key: str = None) -> dict:
        if self.failed_attempts >= self.max_failures:
            return {"status": "FAILED", "reason": "SAFETY_VIOLATION: Max failures reached.", "requires_reset": True}
        
        if not task_prompt or str(task_prompt).strip() == "":
            self.failed_attempts += 1
            return {"status": "FAILED", "reason": "EMPTY_PROMPT", "failed_attempts": self.failed_attempts, "max_attempts": self.max_failures}

        try:
            result = self.run_structured_inference(str(task_prompt))
            self.failed_attempts = 0
            self.verification_passed = True
            return {
                "status": "SUCCESS",
                "answer": result.reasoning,
                "confidence": result.confidence_score
            }
        except Exception as e:
            self.failed_attempts += 1
            return {"status": "FAILED", "reason": str(e), "failed_attempts": self.failed_attempts, "max_attempts": self.max_failures}

    # NEW STRUCTURED METHOD
    def run_structured_inference(
        self, 
        prompt: str, 
        status_callback=None,
        system_instruction: Optional[str] = None, 
        temperature: float = 0.2
    ) -> HarnessEvaluationResult:
        
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
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            if response.parsed:
                return response.parsed
            raise ValueError("Model returned unparseable response.")

        return _execute()

    def _log_retry(self, retry_state, callback):
        attempt = retry_state.attempt_number
        exception = retry_state.outcome.exception()
        wait_time = getattr(retry_state.next_action, "sleep", 0) if retry_state.next_action else 0
        if callback:
            callback(attempt, str(exception), wait_time)

    def reset_harness(self) -> dict:
        self.failed_attempts = 0
        self.verification_passed = False
        return {"status": "RESET", "message": "Harness state reset successfully."}
