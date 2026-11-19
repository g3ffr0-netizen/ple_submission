import os
import sys
from datetime import datetime
from typing import Any, Dict, Tuple

import streamlit as st

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from harness import HarnessExecutionEngine


def load_gemini_key() -> str:
    try:
        key = str(st.secrets["GEMINI_API_KEY"]).strip()
    except (KeyError, FileNotFoundError):
        return ""
    return key


def init_state() -> Tuple[HarnessExecutionEngine, list]:
    if "fair_engine" not in st.session_state:
        st.session_state.fair_engine = HarnessExecutionEngine()
    if "fair_history" not in st.session_state:
        st.session_state.fair_history = []
    return st.session_state.fair_engine, st.session_state.fair_history


def call_gemini(prompt: str, model_name: str, max_tokens: int, api_key: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, vertexai=False)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return (response.text or "").strip()


def verify_response(text: str) -> Tuple[bool, str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "Empty output returned."
    if len(cleaned) < 25:
        return False, "Output too brief; expand with clear details."
    return True, "Verified"


def reset_engine(engine: HarnessExecutionEngine) -> None:
    if hasattr(engine, "reset") and callable(engine.reset):
        engine.reset()
    elif hasattr(engine, "reset_harness") and callable(engine.reset_harness):
        engine.reset_harness()
    else:
        engine.failure_count = 0
        engine.locked = False


def run_task(
    engine: HarnessExecutionEngine,
    prompt: str,
    model_name: str,
    api_key: str,
    dry_run: bool,
) -> Dict[str, Any]:
    if dry_run:
        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return {
            "status": "DRY_RUN_OK",
            "answer": f"[Dry Run] Simulated response for: {preview}",
            "reason": "",
        }

    if hasattr(engine, "model"):
        engine.model = model_name
    if hasattr(engine, "model_name"):
        engine.model_name = model_name

    if hasattr(engine, "run_with_verification") and callable(engine.run_with_verification):
        raw = engine.run_with_verification(
            prompt=prompt,
            generate_fn=lambda p, m, max_tok: call_gemini(p, model_name or m, max_tok, api_key),
            verify_fn=verify_response,
        )
        ok = bool(raw.get("ok", False))
        return {
            "status": "VERIFIED" if ok else str(raw.get("status", "FAILED")).upper(),
            "answer": raw.get("output") or raw.get("error") or raw.get("last_reason") or "",
            "reason": raw.get("last_reason") or raw.get("error") or "",
        }

    raw = engine.run_harness(prompt, api_key=api_key)
    return {
        "status": str(raw.get("status", "UNKNOWN")).upper(),
        "answer": raw.get("answer", ""),
        "reason": raw.get("reason", ""),
    }


def render_sidebar(engine: HarnessExecutionEngine) -> Tuple[str, bool]:
    failures = int(getattr(engine, "failure_count", getattr(engine, "failed_attempts", 0)))
    max_failures = int(getattr(engine, "max_failures", 3))
    locked = bool(getattr(engine, "locked", False))

    with st.sidebar:
        st.header("Engine Telemetry")
        st.metric("Failures", failures)
        st.metric("Failure Cap", max_failures)

        if locked:
            state_badge = "Locked"
        elif failures > 0:
            state_badge = "Retrying"
        else:
            state_badge = "Ready"
        st.metric("Engine State", state_badge)

        st.divider()
        st.subheader("Model & Mode")
        models = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-pro"]
        default_idx = models.index(engine.model) if getattr(engine, "model", None) in models else 0
        selected_model = st.selectbox("Model", models, index=default_idx)
        dry_run = st.checkbox("Dry Run (No API Call)", value=False)
        st.caption("Dry run skips Gemini API calls.")

    return selected_model, dry_run


def main() -> None:
    st.set_page_config(
        page_title="Fair Engine Dashboard",
        page_icon="⚖️",
        layout="wide",
    )
    st.title("⚖️ Fair Engine Dashboard")
    st.caption("Task execution, verification loop, and run history.")

    api_key = load_gemini_key()
    if not api_key:
        st.error("GEMINI_API_KEY is missing or empty in Streamlit Secrets.")
        st.stop()

    engine, history = init_state()
    selected_model, dry_run = render_sidebar(engine)
    is_locked = bool(getattr(engine, "locked", False))

    left_col, right_col = st.columns([2, 1])

    with left_col:
        st.subheader("Task Input")
        user_text = st.text_area(
            "Prompt or task instructions",
            height=150,
            placeholder="Enter a prompt, engineering calculation, or JSON payload...",
        )
        uploaded = st.file_uploader(
            "Attach reference file (optional)",
            type=["csv", "txt", "json", "md"],
        )

        combined_prompt = user_text.strip()
        if uploaded is not None:
            content = uploaded.read().decode("utf-8", errors="ignore").strip()
            if content:
                combined_prompt = (
                    f"{combined_prompt}\n\n--- ATTACHED FILE: {uploaded.name} ---\n{content}"
                    if combined_prompt
                    else f"--- ATTACHED FILE: {uploaded.name} ---\n{content}"
                )

    with right_col:
        st.subheader("Run Controls")

        if st.button("Run Task", use_container_width=True, type="primary"):
            if not combined_prompt:
                st.warning("Enter a prompt or attach a file before running.")
            elif is_locked and not dry_run:
                st.error("Engine is locked. Click Reset Engine to clear the lockout.")
            else:
                with st.spinner("Running verification loop..."):
                    try:
                        outcome = run_task(engine, combined_prompt, selected_model, api_key, dry_run)
                        status = outcome["status"]
                        output = outcome["answer"] or outcome["reason"] or "No output returned."

                        history.append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "model": selected_model,
                            "status": status,
                            "prompt": combined_prompt[:60] + ("..." if len(combined_prompt) > 60 else ""),
                            "output": output,
                        })

                        if status in {"VERIFIED", "DRY_RUN_OK", "SUCCESS"}:
                            st.success(f"Completed ({status})")
                            st.write(output)
                        else:
                            st.warning(f"Completed with status {status}: {output}")
                    except Exception as exc:
                        st.error(f"Execution error: {exc}")

        if st.button("Reset Engine", use_container_width=True):
            reset_engine(engine)
            history.clear()
            st.info("Engine state and history reset.")
            st.rerun()

    st.divider()
    st.subheader("Recent Runs")
    if history:
        for entry in reversed(history[-10:]):
            with st.expander(f"[{entry['time']}] {entry['status']} — {entry['prompt']}"):
                st.write(f"**Model:** {entry['model']}")
                st.write(f"**Status:** {entry['status']}")
                st.write(f"**Output:** {entry['output']}")
    else:
        st.caption("No runs recorded yet.")


if __name__ == "__main__":
    main()
