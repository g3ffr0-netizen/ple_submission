import os
import sys

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine


def get_gemini_api_key() -> str:
    """Read the key from Streamlit secrets or a local environment variable."""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except StreamlitSecretNotFoundError:
        secret_key = ""

    return (secret_key or os.environ.get("GEMINI_API_KEY", "")).strip()


st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="centered")
st.title("🛡️ Sovereign Harness Execution Portal")

API_KEY = get_gemini_api_key()
if not API_KEY:
    st.error(
        "GEMINI_API_KEY is not configured. For local setup, copy "
        "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and add your key. "
        "On Streamlit Community Cloud, add `GEMINI_API_KEY` in the app's Secrets settings."
    )
    st.stop()

if "engine" not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine(api_key=API_KEY)

engine = st.session_state.engine

with st.sidebar:
    st.header("⚙️ Engine Status")
    st.write(f"**Failed Attempts:** {engine.failed_attempts}")
    st.write(f"**Max Failures:** {engine.max_failures}")
    status_icon = "✅" if engine.verification_passed else "❌"
    st.write(f"**Verification Passed:** {status_icon}")


task = st.text_input("Enter Task Prompt:", placeholder="e.g., What is 2+2?")

col1, col2 = st.columns(2)

with col1:
    if st.button("Execute Harness Task", use_container_width=True):
        clean_task = task.strip()
        if not clean_task:
            st.warning("⚠️ Please enter a task prompt before executing.")
        else:
            with st.spinner("Running verification loops..."):
                result = engine.run_harness(clean_task, api_key=API_KEY)

                if result.get("status") == "SUCCESS":
                    st.success(f"✅ **Success:** {result.get('answer')}")
                elif result.get("requires_reset"):
                    st.error(f"🛑 **Lockout:** {result.get('reason')}")
                else:
                    st.warning(
                        f"⚠️ **Failed:** {result.get('reason')} "
                        f"(Attempt {result.get('failed_attempts')}/{engine.max_failures})"
                    )

with col2:
    if st.button("Reset Engine State", use_container_width=True):
        engine.reset_harness()
        st.info("🔄 Engine state has been reset. Failure counter is back to 0.")
        st.rerun()
