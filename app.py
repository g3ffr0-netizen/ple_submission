import json
import os
import sys
from typing import Tuple

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


def get_gemini_api_key() -> str:
    """Read the key from Streamlit secrets or a local environment variable."""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except (StreamlitSecretNotFoundError, Exception):
        secret_key = ""
    return (str(secret_key) or os.environ.get("GEMINI_API_KEY", "")).strip()


def make_generate_fn(api_key: str):
    """Build a generate_fn(prompt, model, max_output_tokens) callback for HarnessExecutionEngine."""
    def _generate(prompt: str, model: str, max_output_tokens: int) -> str:
        if genai is None:
            raise RuntimeError("google-genai is not installed. Add 'google-genai' to requirements.txt.")
        client = genai.Client(api_key=api_key, vertexai=False)
        config = types.GenerateContentConfig(max_output_tokens=max_output_tokens) if types else None
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()

    return _generate


def make_verify_fn(min_chars: int, required_phrase: str, require_json: bool):
    """Build a strict (bool, str) verifier for the harness loop."""
    phrase = required_phrase.strip()

    def _verify(output: str) -> Tuple[bool, str]:
        text = (output or "").strip()
        if not text:
            return False, "Output is empty."
        if len(text) < min_chars:
            return False, f"Output too short ({len(text)} chars; minimum required is {min_chars})."
        if phrase and phrase.lower() not in text.lower():
            return False, f"Output must include the required phrase: '{phrase}'."
        if require_json:
            cleaned = text
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                cleaned = "\n".join(lines[1:-1] if len(lines) >= 3 and lines[-1].strip() == "```" else lines[1:])
            try:
                json.loads(cleaned)
            except json.JSONDecodeError as exc:
                return False, f"Output must be valid JSON ({exc.msg})."
        return True, "All verification checks passed."

    return _verify


st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="wide")
st.title("🛡️ Sovereign Harness Execution Portal")
st.caption("Self-correcting verification loop · Automatic fallback model rotation · Circuit-breaker protection")

API_KEY = get_gemini_api_key()
if not API_KEY:
    st.error(
        "GEMINI_API_KEY is not configured. For local setup, copy "
        "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and add your key. "
        "On Streamlit Community Cloud, add `GEMINI_API_KEY` in the app's Secrets settings."
    )
    st.stop()

if "engine" not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine(config_path="submission.yaml")

if "last_result" not in st.session_state:
    st.session_state.last_result = None

engine: HarnessExecutionEngine = st.session_state.engine

with st.sidebar:
    st.header("⚙️ Engine Status")
    status_label = "🔒 LOCKED" if engine.locked else "🟢 READY"
    st.markdown(f"**State:** {status_label}")
    st.write(f"**Primary Model:** `{engine.model}`")
    if engine.fallback_models:
        st.write(f"**Fallbacks:** `{', '.join(engine.fallback_models)}`")
    st.write(f"**Consecutive Failures:** {engine.failure_count} / {engine.max_failures}")
    st.write(f"**Max Retries / Run:** {engine.max_retries}")

    st.markdown("---")
    st.header
