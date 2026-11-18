import os
import sys
import streamlit as st

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine

st.set_page_config(
    page_title="Sovereign Harness Portal",
    page_icon="🛡️",
    layout="centered",
)
st.title("🛡️ Sovereign Harness Execution Portal")

# Fetch and validate the secret.
try:
    API_KEY = st.secrets["GEMINI_API_KEY"].strip() # Added .strip() to remove hidden spaces
except (KeyError, FileNotFoundError):
    st.error("GEMINI_API_KEY is missing from Streamlit Secrets.")
    st.stop()

if not isinstance(API_KEY, str) or not API_KEY:
    st.error("GEMINI_API_KEY is present but empty or invalid.")
    st.stop()

# Initialize one engine per user session.
if "engine" not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine()

engine = st.session_state.engine

with st.sidebar:
    st.header("⚙️ Engine Status")
    st.write(f"**Failed Attempts:** {engine.failed_attempts}")
    st.write(f"**Max Failures:** {engine.max_failures}")
    status_icon = "✅" if engine.verification_passed else "❌"
    st.write(f"**Verification Passed:** {status_icon}")

MAX_PROMPT_CHARS = 4_000
task = st.text_area(
    "Enter Task Prompt:",
    max_chars=MAX_PROMPT_CHARS,
    placeholder="e.g., What is 2+2?",
)

col1, col2 = st.columns(2)

with col1:
    if st.button("Execute Harness Task", use_container_width=True):
        prompt = task.strip()

        if not prompt:
            st.warning("Please enter a task prompt before executing.")
        else:
            try:
                with st.spinner("Running verification loops..."):
                    result = engine.run_harness(prompt, api_key=API_KEY)

                if not isinstance(result, dict):
                    st.error("The engine returned an unexpected result format.")
                elif result.get("status") == "SUCCESS":
                    st.success(f"✅ Success: {result.get('answer', '')}")
                elif result.get("requires_reset"):
                    st.error(f"🛑 Lockout: {result.get('reason', 'Reset required.')}")
                else:
                    st.warning(
                        f"⚠️ Failed: {result.get('reason', 'No reason provided.')} "
                        f"(Attempt {result.get('failed_attempts', '?')}/"
                        f"{engine.max_failures})"
                    )
            except Exception as e:
                # FIXED: Now you will actually see the real error!
                st.error(f"🚨 **Execution Failed:** {str(e)}")
                # Optional: Log the full traceback to the console, but not the UI
                print(f"Traceback: {e}")

with col2:
    if st.button("Reset Engine State", use_container_width=True):
        engine.reset_harness()
        st.info("🔄 Engine state has been reset.")
        st.rerun()
