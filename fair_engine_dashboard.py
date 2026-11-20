import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import streamlit as st

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    APIError = Exception  # type: ignore[assignment,misc]

try:
    from harness import (
        HarnessExecutionEngine as _ExternalHarnessEngine,
        load_submission_config,
        normalize_model_name,
    )
    _HAS_EXTERNAL_HARNESS = True
except ImportError:
    _HAS_EXTERNAL_HARNESS = False

    def load_submission_config(_path: str = "submission.yaml") -> Dict[str, Any]:
        return {}

    def normalize_model_name(name: Optional[str]) -> str:
        raw = (name or "").strip()
        for prefix in ("models/", "google-cloud/"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
        return raw or "gemini-3.8-flash"


class _FallbackHarnessEngine:
    """Fallback execution engine used only if harness.py cannot be imported."""

    def __init__(
        self,
        model: Optional[str] = None,
        max_retries: int = 3,
        max_failures: int = 3,
        max_output_tokens: int = 4096,
    ) -> None:
        self.model = normalize_model_name(model)
        self.max_retries = max_retries
        self.max_failures = max_failures
        self.max_output_tokens = max_output_tokens
        self.failure_count = 0
        self.locked = False

    def reset(self) -> None:
        self.failure_count = 0
        self.locked = False

    def run_with_verification(
        self,
        prompt: str,
        generate_fn: Callable[[str, str, int], str],
        verify_fn: Callable[[str], Tuple[bool, str]],
    ) -> Dict[str, Any]:
        if self.locked:
            return {
                "ok": False,
                "status": "locked",
                "output": "",
                "attempts": 0,
                "reason": "Engine locked.",
            }

        active_prompt = prompt
        last_output = ""
        last_reason = ""

        for attempt in range(1, self.max_retries + 1):
            last_output = generate_fn(active_prompt, self.model, self.max_output_tokens)
            passed, reason = verify_fn(last_output)
            if passed:
                return {
                    "ok": True,
                    "status": "verified",
                    "output": last_output,
                    "attempts": attempt,
                    "reason": reason,
                }

            last_reason = reason
            self.failure_count += 1
            if self.failure_count >= self.max_failures:
                self.locked = True
                return {
                    "ok": False,
                    "status": "locked",
                    "output": last_output,
                    "attempts": attempt,
                    "reason": last_reason,
                }

            active_prompt = (
                f"{prompt}\n\n[Verification Feedback — Attempt {attempt} failed: {last_reason}. "
                f"Fix the issue and provide a complete response.]"
            )

        return {
            "ok": False,
            "status": "unverified",
            "output": last_output,
            "attempts": self.max_retries,
            "reason": last_reason,
        }


def build_engine(
    model: Optional[str],
    max_retries: int,
    max_failures: int,
    max_output_tokens: int,
) -> Any:
    """Instantiate HarnessExecutionEngine safely across versions of harness.py."""
    if not _HAS_EXTERNAL_HARNESS:
        return _FallbackHarnessEngine(
            model=model,
            max_retries=max_retries,
            max_failures=max_failures,
            max_output_tokens=max_output_tokens,
        )

    try:
        engine = _ExternalHarnessEngine(
            model=model,
            max_retries=max_retries,
            max_failures=max_failures,
            max_output_tokens=max_output_tokens,
        )
    except TypeError:
        engine = _ExternalHarnessEngine(
            model=model,
            max_retries=max_retries,
            max_failures=max_failures,
        )

    if not hasattr(engine, "max_output_tokens"):
        engine.max_output_tokens = max_output_tokens
    return engine


def _has_markdown_table(text: str) -> bool:
    """Detect a basic Markdown table (header row + separator row with pipes)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for i in range(len(lines) - 1):
        if "|" in lines[i] and re.match(r"^[\s|:-]+$", lines[i + 1]) and "-" in lines[i + 1]:
            return True
    return False


def _extract_floats(pattern: str, text: str) -> List[float]:
    """Extract numeric matches for a regex capturing a number group."""
    values: List[float] = []
    for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
        raw_num = match.group(1).replace(",", "").strip()
        try:
            values.append(float(raw_num))
        except ValueError:
            continue
    return values


def verify_response(
    text: str,
    required_terms: Iterable[str] = ("current", "loss", "48 v", "400 v", "verdict"),
    require_table: bool = True,
    min_chars: int = 120,
) -> Tuple[bool, str]:
    """Verify response quality, required terms, table presence, and numerical sanity."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "Empty output returned."

    if len(cleaned) < min_chars:
        return False, f"Output too short ({len(cleaned)} chars); provide a fuller breakdown."

    lowered = cleaned.lower()

    missing_terms = [term for term in required_terms if term.lower() not in lowered]
    if missing_terms:
        return False, f"Missing required sections/terms: {', '.join(missing_terms)}."

    if require_table and not _has_markdown_table(cleaned):
        return False, "Missing a Markdown summary comparison table."

    if "48 v" in lowered or "48v" in lowered:
        amps_48v = _extract_floats(
            r"48\s*v(?![^\n]{0,30}?400\s*v)[^0-9\n]{0,30}?(\d+(?:\.\d+)?)\s*a\b",
            cleaned,
        )
        amps_48v += _extract_floats(r"\b(\d+(?:\.\d+)?)\s*a\b[^0-9\n]{0,25}?48\s*v", cleaned)
        for val in amps_48v:
            if not (180.0 <= val <= 250.0):
                return False, f"Implausible 48 V current value ({val} A) for a 10 kW system."

    if "400 v" in lowered or "400v" in lowered:
        amps_400v = _extract_floats(
            r"400\s*v(?![^\n]{0,30}?48\s*v)[^0-9\n]{0,30}?(\d+(?:\.\d+)?)\s*a\b",
            cleaned,
        )
        amps_400v += _extract_floats(r"\b(\d+(?:\.\d+)?)\s*a\b[^0-9\n]{0,25}?400\s*v", cleaned)
        for val in amps_400v:
            if not (15.0 <= val <= 40.0):
                return False, f"Implausible 400 V current value ({val} A) for a 10 kW system."

    scaling_factors = _extract_floats(
        r"(\d+(?:\.\d+)?)\s*[×x]\s*(?:higher|more|loss|scaling|current)",
        cleaned,
    )
    for factor in scaling_factors:
        if 50.0 <= factor <= 90.0 and not (65.0 <= factor <= 75.0):
            return False, f"Unexpected I^2*R scaling factor ({factor}x); expected ~69.4x."

    return True, "Verified"


def load_gemini_key() -> Optional[str]:
    """Load GEMINI_API_KEY from Streamlit secrets first, then environment variables."""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY")
        if isinstance(secret_key, str) and secret_key.strip():
            return secret_key.strip()
    except Exception:
        pass

    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    return env_key or None


def call_gemini(prompt: str, model_name: str, max_tokens: int, api_key: str) -> str:
    """Call the Gemini API using google-genai and surface API errors cleanly."""
    if genai is None:
        raise RuntimeError("google-genai is not installed. Add `google-genai` to requirements.txt.")

    resolved_model = normalize_model_name(model_name)
    try:
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(max_output_tokens=max_tokens) if types is not None else None
        response = client.models.generate_content(
            model=resolved_model,
            contents=prompt,
            config=config,
        )
        return getattr(response, "text", "") or ""
    except APIError as exc:
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        message = getattr(exc, "message", None) or str(exc)
        details = " — ".join(str(part) for part in (code, status, message) if part)
        raise RuntimeError(f"Gemini API error ({resolved_model}): {details}") from exc


def init_state() -> None:
    """Initialize Streamlit session state and execution engine."""
    cfg = load_submission_config()
    default_model = normalize_model_name(cfg.get("model", "gemini-3.8-flash"))
    max_retries = int(cfg.get("max_retries", 3))
    max_failures = int(cfg.get("max_failures", 3))
    max_tokens = int(cfg.get("max_output_tokens", 4096))

    if "engine" not in st.session_state:
        st.session_state.engine = build_engine(
            model=default_model,
            max_retries=max_retries,
            max_failures=max_failures,
            max_output_tokens=max_tokens,
        )
    if "runs" not in st.session_state:
        st.session_state.runs = []
    if "model_name" not in st.session_state:
        st.session_state.model_name = default_model


def main() -> None:
    st.set_page_config(page_title="Fair Engine Dashboard", layout="wide")
    init_state()

    engine = st.session_state.engine
    api_key = load_gemini_key()

    with st.sidebar:
        st.header("Engine Controls")
        st.session_state.model_name = st.text_input("Model", value=st.session_state.model_name)
        st.metric("Failures", engine.failure_count)
        st.metric("Failure Cap", engine.max_failures)
        st.write("**Engine State:**", "Locked" if engine.locked else "Ready")
        st.write("**API Key:**", "Configured" if api_key else "Missing (`GEMINI_API_KEY`)")

        dry_run = st.checkbox("Dry Run (No API Call)", value=False)
        require_table = st.checkbox("Require Markdown Table", value=True)
        required_terms_raw = st.text_input(
            "Required Terms (comma-separated)",
            value="current, loss, 48 v, 400 v, verdict",
        )

        if st.button("Reset Engine"):
            engine.reset()
            st.rerun()

    st.title("Fair Engine Dashboard")

    default_prompt = (
        "Compare two off-grid solar + LFP battery architectures for a residential property in New South Wales:\n"
        "1. 48 V DC system with a 10 kW inverter and 15 kWh LFP bank\n"
        "2. 400 V DC / high-voltage hybrid system with a 10 kW inverter and 15 kWh LFP bank\n\n"
        "Include nominal battery current at 10 kW discharge, I^2 * R conduction loss scaling, wiring gauge, "
        "safety/isolation, inverter efficiency, a summary table, and a verdict for a 25 m cable run."
    )
    prompt = st.text_area("Task Prompt", value=default_prompt, height=180)
    uploaded = st.file_uploader(
        "Optional context file (.txt, .md, .csv, .json, .yaml)",
        type=["txt", "md", "csv", "json", "yaml", "yml"],
    )

    full_prompt = prompt
    if uploaded is not None:
        try:
            file_text = uploaded.getvalue().decode("utf-8", errors="replace")
            full_prompt = f"{prompt}\n\n[Attached File: {uploaded.name}]\n{file_text}"
        except Exception as exc:
            st.warning(f"Could not read uploaded file: {exc}")

    if st.button("Run Task", type="primary"):
        required_terms = [term.strip() for term in required_terms_raw.split(",") if term.strip()]

        if dry_run:
            sample_output = (
                "### 48 V vs 400 V Architecture\n"
                "At 10 kW discharge, current is ~208.3 A on 48 V and 25.0 A on 400 V. "
                "Conduction loss scales by I^2 * R (~69.4x higher at 48 V for equal resistance).\n\n"
                "| Metric | 48 V | 400 V |\n"
                "| --- | --- | --- |\n"
                "| Current | ~208.3 A | 25.0 A |\n\n"
                "**Verdict:** 400 V is better for a 25 m cable run."
            )
            ok, reason = verify_response(
                sample_output,
                required_terms=required_terms,
                require_table=require_table,
            )
            st.session_state.runs.insert(
                0,
                {
                    "status": "VERIFIED (DRY RUN)" if ok else f"FAILED ({reason})",
                    "output": sample_output,
                },
            )
        elif not api_key:
            st.error("Missing `GEMINI_API_KEY` in Streamlit Secrets or environment variables.")
        else:
            with st.spinner("Running verification loop..."):
                try:
                    result = engine.run_with_verification(
                        prompt=full_prompt,
                        generate_fn=lambda p, m, max_tok: call_gemini(
                            p,
                            st.session_state.model_name or m,
                            max_tok,
                            api_key,
                        ),
                        verify_fn=lambda out: verify_response(
                            out,
                            required_terms=required_terms,
                            require_table=require_table,
                        ),
                    )
                    status_label = (
                        f"VERIFIED (attempt {result.get('attempts', 1)})"
                        if result.get("ok")
                        else f"{str(result.get('status', 'FAILED')).upper()}: {result.get('reason', '')}"
                    )
                    st.session_state.runs.insert(
                        0,
                        {
                            "status": status_label,
                            "output": result.get("output", ""),
                        },
                    )
                except RuntimeError as exc:
                    st.error(str(exc))

    st.subheader("Recent Runs")
    if not st.session_state.runs:
        st.info("No runs yet. Click **Run Task** to start.")
    else:
        for idx, run in enumerate(st.session_state.runs, start=1):
            with st.expander(f"Run #{idx} — {run['status']}", expanded=(idx == 1)):
                st.markdown(run["output"] or "_No output_")


if __name__ == "__main__":
    main()
