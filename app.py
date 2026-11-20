import json
import os
from typing import Tuple

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

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
    except StreamlitSecretNotFoundError:
        secret_key = ""
    return (str(secret_key) or os.environ.get("GEMINI_API_KEY", "")).strip()


def make_generate_fn(api_key: str):
    """Build the generation callback expected by HarnessExecutionEngine."""
    def _generate(prompt: str, model: str, max_output_tokens: int) -> str:
        if genai is None:
            raise RuntimeError(
                "google-genai is not installed. Install dependencies from requirements.txt."
            )
        client = genai.Client(api_key=api_key, vertexai=False)
        config = (
            types.GenerateContentConfig(max_output_tokens=max_output_tokens)
            if types
            else None
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()

    return _generate


def make_verify_fn(min_chars: int, required_phrase: str, require_json: bool):
    """Build a verifier for the optional output constraints in the portal."""
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
                cleaned = "\n".join(
                    lines[1:-1]
                    if len(lines) >= 3 and lines[-1].strip() == "```"
                    else lines[1:]
                )
            try:
                json.loads(cleaned)
            except json.JSONDecodeError as exc:
                return False, f"Output must be valid JSON ({exc.msg})."
        return True, "All verification checks passed."

    return _verify


st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="wide")
st.title("🛡️ Sovereign Harness Execution Portal")
st.caption(
    "Self-correcting verification loop · Automatic fallback model rotation · "
    "Circuit-breaker protection"
)

if "engine" not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine(config_path="submission.yaml")
if "last_result" not in st.session_state:
    st.session_state.last_result = None

engine: HarnessExecutionEngine = st.session_state.engine
api_key = get_gemini_api_key()

with st.sidebar:
    st.header("⚙️ Engine Status")
    st.markdown(f"**State:** {'🔒 LOCKED' if engine.locked else '🟢 READY'}")
    st.write(f"**Primary model:** `{engine.model}`")
    if engine.fallback_models:
        st.write(f"**Fallbacks:** `{', '.join(engine.fallback_models)}`")
    st.write(f"**Consecutive failures:** {engine.failure_count} / {engine.max_failures}")
    st.write(f"**Max retries per run:** {engine.max_retries}")
    st.write(f"**API key:** {'Configured' if api_key else 'Not configured'}")

    if st.button("Reset locked harness", disabled=not engine.locked, use_container_width=True):
        engine.reset()
        st.session_state.last_result = None
        st.rerun()

if not api_key:
    st.warning(
        "Add `GEMINI_API_KEY` to Streamlit Secrets or the environment to enable runs. "
        "For local setup, copy `.streamlit/secrets.toml.example` to "
        "`.streamlit/secrets.toml` and replace the placeholder value."
    )
elif genai is None:
    st.error("The Google GenAI package is unavailable. Install dependencies from requirements.txt.")

st.subheader("Run a task")
prompt = st.text_area(
    "Task prompt",
    placeholder="Describe the task you want Gemini to complete…",
    height=180,
)

with st.expander("Verification requirements", expanded=False):
    min_chars = st.number_input("Minimum output length (characters)", min_value=1, value=1, step=1)
    required_phrase = st.text_input("Required phrase (optional)")
    require_json = st.checkbox("Require valid JSON output")

can_run = bool(api_key and genai is not None and not engine.locked and prompt.strip())
if st.button("Run and verify", type="primary", disabled=not can_run):
    verify_fn = make_verify_fn(int(min_chars), required_phrase, require_json)
    with st.spinner("Generating and checking the response…"):
        result = engine.run_with_verification(
            prompt,
            make_generate_fn(api_key),
            verify_fn,
        )
    st.session_state.last_result = result

result = st.session_state.last_result
if result:
    st.subheader("Latest result")
    if result.get("ok"):
        st.success(f"Verified on attempt {result.get('attempt', '?')} using `{result.get('model', '')}`.")
        st.markdown(result.get("output", ""))
    else:
        st.error(result.get("error") or result.get("last_reason") or "The task could not be verified.")
        if result.get("output"):
            with st.expander("Last generated output"):
                st.text(result["output"])

    history = result.get("history", [])
    if history:
        st.subheader("Attempt history")
        st.dataframe(pd.DataFrame(history), use_container_width=True, hide_index=True)
    st.caption(
        f"Status: {result.get('status', 'unknown')} · "
        f"Elapsed: {result.get('elapsed_ms', 0)} ms · "
        f"Consecutive failures: {engine.failure_count}"
    )
