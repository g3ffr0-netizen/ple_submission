from google import genai
from google.genai import types
from google.genai.errors import APIError

class HarnessExecutionEngine:
    def __init__(self, model_name: str = "gemini-2.0-flash"):
        self.client = genai.Client()
        self.model_name = model_name

    def run_inference(
        self, 
        prompt: str, 
        system_instruction: str | None = None, 
        temperature: float = 0.7
    ) -> str:
        """
        Runs inference with try/except error handling for GenAI API errors.
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            return response.text or ""
            
        except APIError as e:
            # Handle specific API errors (e.g., quota limits, invalid keys, server errors)
            error_message = f"Gemini API Error [{e.code}]: {e.message}"
            print(error_message) # Logs to terminal/Streamlit logs
            return f"⚠️ **Execution Failed:** {error_message}"
            
        except Exception as e:
            # Handle any other unexpected runtime exceptions (e.g., network drops)
            error_message = f"Unexpected Error: {str(e)}"
            print(error_message)
            return f"⚠️ **Execution Failed:** {error_message}"
