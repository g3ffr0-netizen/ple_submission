import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import streamlit as st

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    APIError = Exception  # type: ignore[assignment,misc]

try:
    from harness import (
        HarnessExecutionEngine as _ExternalHarnessEngine,
        load_submission_config,
        normalize_model_name,
    )
    _HAS_EXTERNAL_HARNESS = True
except ImportError:
    _HAS_EXTERNAL_HARNESS = False

    def load_submission_config(_path: str = "submission.yaml") -> Dict[str, Any]:
        return {}

    def normalize_model_name(name: Optional[str], fallback: str = "gemini-3.8-flash") -> str:
        raw = (name or "").strip()
        cleaned = raw.split("/")[-1].strip()
        return cleaned or fallback


class _FallbackHarnessEngine:
    """Fallback execution engine used only if harness.py cannot be imported."""

    def __init__(
        self,
        model: Optional[str] = None,
        max_retries: int = 3,
        max_failures: int = 3,
        max_output_tokens: int = 4096,
        fallback_models: Optional[List[str]] = None,
    ) -> None:
        self.model = normalize_model_name(model)
        self.max_retries = max(1, int(max_retries))
        self.max_failures = max(1, int(max_failures))
        self.max_output_tokens = max(64, int(max_output_tokens))
        self.fallback_models = [normalize_model_name(m) for m in (fallback_models or []) if m]
        self.failure_count = 0
        self.locked = False

    def reset(self) -> Dict[str, Any]:
        self.failure_count = 0
        self.locked = False
        return {"status": "reset", "locked": False, "failure_count": 0}

    def run_with_verification(
        self,
        prompt: str,
        generate_fn: Callable[[str, str, int], str],
        verify_fn: Callable[[str], Tuple[bool, str]],
    ) -> Dict[str, Any]:
        if self.locked:
            return {
                "ok": False,
                "status": "locked",
                "output": "",
                "attempt": 0,
                "model": self.model,
                "last_reason": "Engine locked.",
                "error": "Engine locked.",
                "elapsed_ms": 0.0,
                "history": [],
            }

        active_prompt = prompt
        last_output = ""
        last_reason = ""
        history: List[Dict[str, Any]] = []

        for attempt in range(1, self.max_retries + 1):
            last_output = generate_fn(active_prompt, self.model, self.max_output_tokens)
            passed, reason = verify_fn(last_output)
            history.append({
                "attempt": attempt,
                "model": self.model,
                "passed": passed,
                "reason": reason,
                "elapsed_ms": 0.0,
                "error": None,
            })
            if passed:
                self.failure_count = 0
                return {
                    "ok": True,
                    "status": "verified",
                    "output": last_output,
                    "attempt": attempt,
                    "model": self.model,
                    "last_reason": reason,
                    "elapsed_ms": 0.0,
                    "history": history,
                }

            last_reason = reason
            active_prompt = (
                f"{prompt}\n\n[Verification Feedback — Attempt {attempt} failed: {last_reason}. "
                f"Fix the issue and provide a complete response.]"
            )

        self.failure_count += 1
        if self.failure_count >= self.max_failures:
            self.locked = True
