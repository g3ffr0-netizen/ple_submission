class HarnessExecutionEngine:
    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-2.0-flash", max_failures: int = 3):
        self.model_name = model_name
        self.max_failures = max_failures
        self.failed_attempts = 0
        self.verification_passed = False
        self._test_failure_count = 0
        
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if resolved_key:
            self.client = genai.Client(api_key=resolved_key, vertexai=False)
        else:
            self.client = None
