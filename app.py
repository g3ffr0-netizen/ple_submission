import streamlit as st
from harness import HarnessExecutionEngine

st.title("🛡️ Sovereign Harness Execution Portal")

# Initialize session state variables
if "execution_history" not in st.session_state:
    st.session_state.execution_history = []

if "total_attempts_logged" not in st.session_state:
    st.session_state.total_attempts_logged = 0

# Sidebar metrics & testing toggles
st.sidebar.title("📊 Session Metrics")
st.sidebar.metric("Total Executions", len(st.session_state.execution_history))
st.sidebar.metric("Total Attempts Recorded", st.session_state.total_attempts_logged)

st.sidebar.markdown("---")
simulate_test_error = st.sidebar.checkbox("🧪 Simulate API Rate Limit", value=False)

# Main task input
task_prompt = st.text_area(
    "Enter Evaluation Prompt:", 
    "Evaluate whether the following assistant response correctly answers the user:\nUser: What is the capital of France?\nAssistant: The capital of France is Paris."
)

if st.button("Run Structured Harness"):
    engine = HarnessExecutionEngine()
    
    attempt_container = [0]

    def count_attempt(attempt_num, error_msg, wait_seconds):
        attempt_container[0] = attempt_num

    with st.status("Executing structured harness inference...", expanded=True) as status:
        try:
            result = engine.run_structured_inference(
                prompt=task_prompt, 
                status_callback=count_attempt,
                simulate_errors=simulate_test_error
            )
            
            attempt_count = attempt_container[0]
            if attempt_count == 0:
                attempt_count = 1
                
            status.update(label="✅ Evaluation Successful!", state="complete", expanded=False)
            
            # Display structured Pydantic results
            st.success("Structured Evaluation Results:")
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Success", str(result.is_successful))
            with col2:
                st.metric("Confidence Score", f"{result.confidence_score * 100:.1f}%")
            
            st.write(f"**Reasoning:** {result.reasoning}")
            
            # Save to session history
            st.session_state.execution_history.append({
                "prompt": task_prompt,
                "result": f"Success: {result.is_successful} | Confidence: {result.confidence_score} | {result.reasoning}",
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

# Display past execution history logs
if st.session_state.execution_history:
    st.markdown("---")
    st.subheader("📜 Past Execution History")
    for idx, item in enumerate(reversed(st.session_state.execution_history)):
        with st.expander(f"Run #{len(st.session_state.execution_history) - idx}: {item['status']} (Attempts: {item['attempts']})"):
            st.write(f"**Prompt:** {item['prompt']}")
            st.write(f"**Outcome:** {item['result']}")
