# =====================================================================
# SOVEREIGN HARNESS EXECUTION INTERFACE (app.py)
# Integrated with harness.py Safety Framework
# =====================================================================

import streamlit as st
import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

try:
    from harness import HarnessExecutionEngine
    HAS_HARNESS = True
except ImportError:
    HAS_HARNESS = False

st.set_page_config(page_title="Sovereign Harness Portal", page_icon="🛡️", layout="centered")
st.title("🛡️ Sovereign Harness Execution Portal")

user_input = st.text_input("Type your harness task or query here...")

if st.button("Execute Harness Task", use_container_width=True):
    if user_input.strip():
        if HAS_HARNESS:
            engine = HarnessExecutionEngine()
            result = engine.run_harness(user_input)
            st.json(result)
        else:
            st.warning("harness.py module not found in directory.")
    else:
        st.warning("Please enter a valid task.")
