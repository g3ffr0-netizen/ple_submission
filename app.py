import streamlit as st
from harness import HarnessExecutionEngine

st.title("🛡️ Sovereign Harness Execution Portal")

task_prompt = st.text_input("Enter Task Prompt:", "What is 2+2?")

if st.button("Run Harness"):
    engine = HarnessExecutionEngine()
    
    # st.spinner keeps the animation spinning during the entire execution 
    # (including any tenacity retry backoff delays)
    with st.spinner("Executing harness inference (retrying if needed)..."):
        try:
            result = engine.run_inference(prompt=task_prompt)
            st.success("Success!")
            st.write(result)
        except Exception as e:
            # If all tenacity retries fail, it reraises the exception here
            st.error(f"⚠️ **All retries failed:** {str(e)}")
