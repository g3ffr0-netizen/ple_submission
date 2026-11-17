import streamlit as st
from harness import HarnessExecutionEngine

st.title("🛡️ Sovereign Harness Execution Portal")

task_prompt = st.text_input("Enter Task Prompt:", "What is 2+2?")

if st.button("Run Harness"):
    engine = HarnessExecutionEngine()
    
    with st.spinner("Executing harness inference..."):
        result = engine.run_inference(prompt=task_prompt)
        
    if result.startswith("⚠️"):
        st.error(result)
    else:
        st.success("Success!")
        st.write(result)
