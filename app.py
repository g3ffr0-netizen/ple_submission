import streamlit as st
from harness import HarnessExecutionEngine

st.title("🛡️ Sovereign Harness Execution Portal")

task_prompt = st.text_input("Enter Task Prompt:", "What is 2+2?")

if st.button("Run Harness with Live Tracking"):
    engine = HarnessExecutionEngine()
    
    # Create a live status container
    with st.status("Executing harness inference...", expanded=True) as status:
        
        # Define the callback function that tenacity will call on failure
        def update_ui_status(attempt_num, error_msg, wait_seconds):
            status.write(f"⚠️ Attempt {attempt_num} failed: `{error_msg}`")
            status.write(f"⏳ Backing off for {int(wait_seconds)} seconds before retry...")
        
        try:
            # Run inference and pass the callback
            result = engine.run_inference_with_status(
                prompt=task_prompt, 
                status_callback=update_ui_status
            )
            
            # If successful, update the status box to success state
            status.update(label="✅ Execution Successful!", state="complete", expanded=False)
            st.success("Result:")
            st.write(result)
            
        except Exception as e:
            # If all attempts fail
            status.update(label="❌ Execution Failed After All Retries", state="error", expanded=True)
            st.error(f"Final Error: {str(e)}")
