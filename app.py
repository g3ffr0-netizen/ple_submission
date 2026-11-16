import streamlit as st
import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine

st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="centered")
st.title("🛡️ Sovereign Harness Execution Portal")

# --- HARDCODED API KEY ASSIGNMENT ---
API_KEY = "AQ.Ab8RN6K6qnt8f7wMmVG3epeZoFwDbXxz4xxbm3Q_eh2MrNqdLg"

if not API_KEY or API_KEY == "PASTE_YOUR_KEY_HERE":
    st.error("🚨 Please provide a valid API key on line 12 of app.py!")
    st.stop()

if 'engine' not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine()

engine = st.session_state.engine

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Engine Status")
    st.write(f"**Failed Attempts:** {engine.failed_attempts}")
    st.write(f"**Max Failures:** {engine.max_failures}")
    status_icon = "✅" if engine.verification_passed else "❌"
    st.write(f"**Verification Passed:** {status_icon}")

# --- MAIN PAGE ---
task = st.text_input("Enter Task Prompt:", placeholder="e.g., What is 2+2?")

col1, col2 = st.columns(2)

with col1:
    if st.button("Execute Harness Task", use_container_width=True):
        if not task.strip():
            st.warning("⚠️ Please enter a task prompt before executing.")
        else:
            with st.spinner("Running verification loops..."):
                result = engine.run_harness(task, api_key=API_KEY)
                
                if result.get("status") == "SUCCESS":
                    st.success(f"✅ **Success:** {result.get('answer')}")
                elif result.get("requires_reset"):
                    st.error(f"🛑 **Lockout:** {result.get('reason')}")
                else:
                    st.warning(f"⚠️ **Failed:** {result.get('reason')} (Attempt {result.get('failed_attempts')}/{engine.max_failures})")

with col2:
    if st.button("Reset Engine State", use_container_width=True):
        engine.reset_harness()
        st.info("🔄 Engine state has been reset. Failure counter is back to 0.")
        st.rerun()
