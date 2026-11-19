import streamlit as st
import sys
import os
from datetime import datetime

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine

st.set_page_config(
    page_title="Fair Engine Dashboard",
    page_icon="⚖️",
    layout="wide"
)

st.title("⚖️ Fair Engine Machine Dashboard")
st.markdown("*Advanced analytics, file processing, and verification powered by the Fair Engine.*")

# --- SECURE KEY FETCH ---
try:
    API_KEY = st.secrets["GEMINI_API_KEY"].strip()
except (KeyError, FileNotFoundError):
    st.error("🚨 GEMINI_API_KEY not found in Streamlit Secrets.")
    st.stop()

if not API_KEY:
    st.error("🚨 GEMINI_API_KEY is empty or invalid.")
    st.stop()

# --- SESSION STATE ---
if 'engine' not in st.session_state:
    st.session_state.engine = HarnessExecutionEngine()

if 'history' not in st.session_state:
    st.session_state.history = []

engine = st.session_state.engine

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Engine Status")
    st.metric("Failed Attempts", engine.failed_attempts)
    st.metric("Max Failures", engine.max_failures)
    
    status = "✅ Active" if engine.verification_passed else "❌ Idle"
    st.metric("System Status", status)
    
    st.markdown("---")
    st.subheader("🎛️ Model Settings")
    model_choice = st.selectbox(
        "Select Model",
        ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-pro"],
        index=0
    )
    
    dry_run = st.checkbox("Dry Run (No API Call)", value=False)
    st.caption("Dry Run simulates the engine without burning API credits.")
    
    st.markdown("---")
    st.caption("Fair Engine v1.0")

# --- MAIN DASHBOARD ---
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📝 Input Data")
    task_prompt = st.text_area(
        "Enter your query or task data:",
        height=150,
        placeholder="Paste your prompt, data, or JSON here..."
    )
    
    uploaded_file = st.file_uploader(
        "Or upload a file (CSV, PDF, TXT):",
        type=["csv", "pdf", "txt", "json"]
    )
    
    if uploaded_file is not None:
        st.info(f"📎 File attached: {uploaded_file.name} ({uploaded_file.size} bytes)")
        # For now, just read as text. The UltimateEncoder will handle this later.
        file_content = uploaded_file.read().decode("utf-8", errors="ignore")
        task_prompt = f"{task_prompt}\n\n--- FILE CONTENT ---\n{file_content}"

with col2:
    st.subheader("🎮 Controls")
    st.write("Configure and execute the Fair Engine.")
    
    if st.button("🚀 Execute Task", use_container_width=True, type="primary"):
        if not task_prompt.strip():
            st.warning("⚠️ Please enter some data first.")
        else:
            with st.spinner("Fair Engine is processing..."):
                try:
                    if dry_run:
                        result = {
                            "status": "SUCCESS",
                            "answer": f"[DRY RUN] Simulated response for: {task_prompt[:50]}...",
                            "confidence": 0.99
                        }
                    else:
                        # Temporarily override the model name if the engine supports it
                        if hasattr(engine, 'model_name'):
                            engine.model_name = model_choice
                        
                        result = engine.run_harness(task_prompt, api_key=API_KEY)
                    
                    # Log to history
                    st.session_state.history.append({
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "prompt": task_prompt[:50] + "..." if len(task_prompt) > 50 else task_prompt,
                        "model": model_choice,
                        "status": result.get("status", "UNKNOWN"),
                        "answer": result.get("answer", result.get("reason", ""))
                    })
                    
                    if result.get("status") == "SUCCESS":
                        st.success("✅ Success!")
                        st.write(result.get("answer", result))
                    else:
                        st.warning(f"⚠️ Result: {result.get('reason', result)}")
                        
                except Exception as e:
                    st.error(f"🚨 Error: {str(e)}")

    if st.button("🔄 Reset Engine", use_container_width=True):
        engine.reset_harness()
        st.session_state.history = []
        st.info("Engine and history reset.")
        st.rerun()

# --- RESULTS DISPLAY ---
st.markdown("---")
st.subheader("📊 Execution History")

if st.session_state.history:
    for item in reversed(st.session_state.history[-10:]):
        with st.expander(f"[{item['timestamp']}] {item['status']} - {item['prompt']}"):
            st.write(f"**Model:** {item['model']}")
            st.write(f"**Status:** {item['status']}")
            st.write(f"**Answer:** {item['answer']}")
else:
    st.info("No history yet. Run a task to see results here.")
