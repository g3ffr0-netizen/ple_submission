import os
from typing import Any, Dict

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from harness import HarnessExecutionEngine, extract_json_payload, make_verify_fn

_extract_json_payload = extract_json_payload

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
    client = genai.Client(api_key=api_key, vertexai=False) if genai is not None else None

    def _generate(prompt: str, model: str, max_output_tokens: int) -> str:
        if genai is None or client is None:
            raise RuntimeError(
                "google-genai is not installed. Install dependencies from requirements.txt."
            )
        config = (
            types.GenerateContentConfig(max_output_tokens=max_output_tokens)
            if types is not None and max_output_tokens
            else None
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()

    return _generate


def _init_session_state() -> None:
    """Initialize required Streamlit session state keys once per session."""
    if "last_result" not in st.session_state:
        st.session_state.last_result = None
    if "is_running" not in st.session_state:
        st.session_state.is_running = False
    if "ui_error" not in st.session_state:
        st.session_state.ui_error = None
    if "engine" not in st.session_state:
        try:
            st.session_state.engine = HarnessExecutionEngine(
                config_path="submission.yaml"
            )
        except Exception as exc:
            st.session_state.engine = None
            st.session_state.ui_error = f"Failed to load submission.yaml: {exc}"


def _render_sidebar(engine: Any, api_key: str) -> None:
    """Render engine status and controls in the sidebar."""
    with st.sidebar:
        st.header("⚙️ Engine Status")
        if engine is None:
            st.markdown("**State:** 🔴 UNAVAILABLE")
            st.write(f"**API key:** {'Configured' if api_key else 'Not configured'}")
            if st.button("Retry engine init", disabled=st.session_state.is_running):
                st.session_state.pop("engine", None)
                st.session_state.ui_error = None
                st.rerun()
            return

        st.markdown(f"**State:** {'🔒 LOCKED' if engine.locked else '🟢 READY'}")
        st.write(f"**Primary model:** `{engine.model}`")
        if getattr(engine, "fallback_models", None):
            st.write(f"**Fallbacks:** `{', '.join(engine.fallback_models)}`")
        st.write(
            f"**Consecutive failures:** {engine.failure_count} / {engine.max_failures}"
        )
        st.write(f"**Max retries per run:** {engine.max_retries}")
        st.write(f"**API key:** {'Configured' if api_key else 'Not configured'}")

        if st.button(
            "Reset locked harness",
            disabled=not engine.locked or st.session_state.is_running,
        ):
            engine.reset()
            st.session_state.last_result = None
            st.session_state.ui_error = None
            st.rerun()

        if st.button(
            "Clear result",
            disabled=st.session_state.last_result is None or st.session_state.is_running,
        ):
            st.session_state.last_result = None
            st.rerun()


def _render_result(result: Dict[str, Any], engine: Any) -> None:
    """Render the latest run outcome and attempt history."""
    st.subheader("Latest result")
    if result.get("ok"):
        st.success(
            f"Verified on attempt {result.get('attempt', '?')} using `{result.get('model', '')}`."
        )
        st.markdown(result.get("output", ""))
    else:
        st.error(
            result.get("error")
            or result.get("last_reason")
            or "The task could not be verified."
        )
        if result.get("output"):
            with st.expander("Last generated output"):
                st.text(result["output"])

    history = result.get("history", [])
    if history:
        st.subheader("Attempt history")
        st.dataframe(pd.DataFrame(history), hide_index=True)

    failure_count = engine.failure_count if engine is not None else "n/a"
    st.caption(
        f"Status: {result.get('status', 'unknown')} · "
        f"Elapsed: {result.get('elapsed_ms', 0)} ms · "
        f"Consecutive failures: {failure_count}"
    )


def main() -> None:
    st.set_page_config(
        page_title="Sovereign Harness Portal",
        page_icon="🛡️",
        layout="wide",
    )
    st.title("🛡️ Sovereign Harness Execution Portal")
    st.caption(
        "Self-correcting verification loop · Automatic fallback model rotation · "
        "Circuit-breaker protection"
    )

    _init_session_state()
    engine = st.session_state.engine
    api_key = get_gemini_api_key()
    _render_sidebar(engine, api_key)

    if not api_key:
        st.warning(
            "Add `GEMINI_API_KEY` to Streamlit Secrets or the environment to enable runs. "
            "For local setup, copy `.streamlit/secrets.toml.example` to "
            "`.streamlit/secrets.toml` and replace the placeholder value."
        )
    elif genai is None:
        st.error(
            "The Google GenAI package is unavailable. Install dependencies from requirements.txt."
        )

    if st.session_state.ui_error:
        st.error(st.session_state.ui_error)

    st.subheader("Run a task")
    prompt = st.text_area(
        "Task prompt",
        placeholder="Describe the task you want Gemini to complete…",
        height=180,
        disabled=st.session_state.is_running or engine is None,
    )

    with st.expander("Verification requirements", expanded=False):
        min_chars = st.number_input(
            "Minimum output length (characters)",
            min_value=1,
            value=1,
            step=1,
            disabled=st.session_state.is_running or engine is None,
        )
        required_phrase = st.text_input(
            "Required phrase (optional)",
            disabled=st.session_state.is_running or engine is None,
        )
        require_json = st.checkbox(
            "Require valid JSON output",
            disabled=st.session_state.is_running or engine is None,
        )

    can_run = bool(
        engine is not None
        and api_key
        and genai is not None
        and not engine.locked
        and not st.session_state.is_running
        and prompt.strip()
    )

    if st.button("Run and verify", type="primary", disabled=not can_run):
        st.session_state.is_running = True
        st.session_state.ui_error = None
        verify_fn = make_verify_fn(int(min_chars), required_phrase, require_json)
        generate_fn = make_generate_fn(api_key)
        try:
            with st.spinner("Generating and checking the response…"):
                result = engine.run_with_verification(
                    prompt,
                    generate_fn,
                    verify_fn,
                )
            st.session_state.last_result = result
        except Exception as exc:
            st.session_state.ui_error = f"Execution error: {exc}"
        finally:
            st.session_state.is_running = False
            st.rerun()

    if st.session_state.last_result:
        _render_result(st.session_state.last_result, engine)


if __name__ == "__main__":
    main()
