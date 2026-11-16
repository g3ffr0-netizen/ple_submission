import streamlit as st
import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

st.write("✅ Imports loaded")

try:
    from harness import HarnessExecutionEngine
    st.write("✅ Harness imported successfully!")
    
    # Try to initialize it
    if 'engine' not in st.session_state:
        st.session_state.engine = HarnessExecutionEngine()
    st.write("✅ Engine initialized!")
    
except Exception as e:
    st.error(f"🚨 CRASH ON IMPORT/INIT: {e}")
