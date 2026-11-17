import os
from google import genai
from google.genai import types

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        # The SDK automatically pulls GEMINI_API_KEY from Streamlit secrets or environment variables
        self.client = genai.Client()
        self.model_name = model_name

    def run_inference(self, prompt: str, system_instruction: str = None) -> str:
        config = None
        if system_instruction:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
            )

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config,
        )
        return response.text
