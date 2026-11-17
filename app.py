import streamlit as st
from harness import HarnessExecutionEngine

st.title("🛡️ Sovereign Harness Execution Portal")

# Initialize session state variables
if "execution_history" not in st.session_state:
    st.session_state.execution_history = []

if "total_attempts_logged" not in st.session_state:
    st.session_state.total_attempts_logged = 0

# Sidebar metrics
st.sidebar.title("📊 Session Metrics")
st.sidebar.metric("Total Executions", len(st.session_state.execution_history))
st.sidebar.metric("Total Attempts Recorded", st.session_state.total_attempts_logged)

task_prompt = st.text_input("Enter Task Prompt:", "What is 2+2?")

if st.button("Run Harness with History Tracking"):
    engine = HarnessExecutionEngine()
    
    attempt_container = [0]

    def count_attempt(attempt_num, error_msg, wait_seconds):
        attempt_container[0] = attempt_num

    with st.status("Executing harness inference...", expanded=True) as status:
        try:
            result = engine.run_inference_with_status(
                prompt=task_prompt, 
                status_callback=count_attempt
            )
            
            attempt_count = attempt_container[0]
            if attempt_count == 0:
                attempt_count = 1
                
            status.update(label="✅ Success!", state="complete", expanded=False)
            st.success("Result:")
            st.write(result)
            
            st.session_state.execution_history.append({
                "prompt": task_prompt,
                "result": result,
                "attempts": attempt_count,
                "status": "Success"
            })
            st.session_state.total_attempts_logged += attempt_count
            
        except Exception as e:
            failed_attempts = attempt_container[0] or 3
            st.session_state.execution_history.append({
                "prompt": task_prompt,
                "result": str(e),
                "attempts": failed_attempts,
                "status": "Failed"
            })
            st.session_state.total_attempts_logged += failed_attempts
            status.update(label="❌ Execution Failed", state="error", expanded=True)
            st.error(f"Error: {str(e)}")

# Display history
if st.session_state.execution_history:
    st.markdown("---")
    st.subheader("📜 Past Execution History")
    for idx, item in enumerate(reversed(st.session_state.execution_history)):
        with st.expander(f"Run #{len(st.session_state.execution_history) - idx}: {item['prompt']} ({item['status']})"):
            st.write(f"**Prompt:** {item['prompt']}")
            st.write(f"**Attempts Required:** {item['attempts']}")
            st.write(f"**Outcome:** {item['result']}")
