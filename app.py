import os
st.write("--- DIAGNOSTIC ---")
st.write("Is the key in the environment?", "YES" if os.environ.get("GEMINI_API_KEY") else "NO")
if os.environ.get("GEMINI_API_KEY"):
    st.write("Key starts with:", os.environ.get("GEMINI_API_KEY")[:5])
st.write("------------------")import streamlit as st
import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine

# Page configuration
st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="centered")

# Initialize the engine
if 'engine' not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine()

engine = st.session_state.engine

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Engine Status")
    st.write(f"**Failed Attempts:** {engine.failed_attempts}")
    st.write(f"**Max Failures:** {engine.max_failures}")
    
    # Safely display the boolean
    status_icon = "✅" if engine.verification_passed else "❌"
    st.write(f"**Verification Passed:** {status_icon}")

# --- MAIN PAGE ---
st.title("🛡️ Sovereign Harness Execution Portal")
st.markdown("*Submit tasks directly to the verified execution harness with automated safety gates.*")

# Input box
task = st.text_input("Enter Task Prompt:", placeholder="e.g., Run telemetry check or fail test")

# Buttons
col1, col2 = st.columns(2)

with col1:
    if st.button("Execute Harness Task", use_container_width=True):
        if not task.strip():
            st.warning("⚠️ Please enter a task prompt before executing.")
        else:
            with st.spinner("Running verification loops..."):
                result = engine.run_harness(task)
                
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
