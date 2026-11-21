import json
import pytest

from app import _extract_json_payload, make_verify_fn
from harness import HarnessExecutionEngine


# --- 1. JSON fence extraction & verification boundaries ---

def test_extract_json_payload_strips_fenced_block():
    raw = "```json\n{\"status\": \"ok\", \"bus_kv\": 1.0}\n```"
    parsed = json.loads(_extract_json_payload(raw))
    assert parsed == {"status": "ok", "bus_kv": 1.0}


def test_verify_rejects_malformed_json_even_with_required_phrase():
    verify = make_verify_fn(min_chars=10, required_phrase="microgrid verified", require_json=True)
    ok, reason = verify("microgrid verified and operational {bad json")
    assert not ok
    assert "json" in reason.lower()


def test_verify_min_chars_exact_boundary():
    payload = '{"status":"ok","note":"microgrid verified and operational"}'
    verify_exact = make_verify_fn(min_chars=len(payload), required_phrase="", require_json=True)
    verify_over = make_verify_fn(min_chars=len(payload) + 1, required_phrase="", require_json=True)

    assert verify_exact(payload)[0] is True
    assert verify_over(payload)[0] is False


def test_verify_phrase_case_insensitive_and_missing():
    verify = make_verify_fn(min_chars=1, required_phrase="Microgrid Verified", require_json=False)
    assert verify("prefix microgrid verified suffix")[0] is True
    assert verify("missing the target text")[0] is False


# --- 2. Retry feedback, fallback rotation & circuit-breaker lifecycle ---

def test_retry_prompt_carries_failure_reason_and_rotates_models():
    seen_calls = []

    def fake_generate(prompt: str, model: str, max_output_tokens: int) -> str:
        seen_calls.append((model, prompt, max_output_tokens))
        if len(seen_calls) == 1:
            return '{"status": "ok"}'  # too short for min_chars=120
        return '{"status": "ok", "detail": "padded response that easily exceeds the minimum length requirement"}'

    verify = make_verify_fn(min_chars=120, required_phrase="", require_json=True)
    engine = HarnessExecutionEngine.__new__(HarnessExecutionEngine)
    engine.model = "gemini-4-hydrogen"
    engine.fallback_models = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    engine.max_retries = 3
    engine.max_failures = 3
    engine.max_output_tokens = 4096
    engine.failure_count = 0
    engine.locked = False

    if hasattr(engine, "_state_lock"):
        import threading
        engine._state_lock = threading.Lock()

    result = engine.run_with_verification("return json", fake_generate, verify)

    assert result["ok"] is True
    assert len(seen_calls) >= 2
    assert seen_calls[0][0] == "gemini-4-hydrogen"
    assert "minimum required is 120" in seen_calls[1][1].lower()


def test_circuit_breaker_locks_after_max_failures_and_resets_cleanly():
    def always_bad(prompt: str, model: str, max_output_tokens: int) -> str:
        return "not json"

    verify = make_verify_fn(min_chars=1, required_phrase="impossible_phrase_xyz", require_json=True)
    engine = HarnessExecutionEngine.__new__(HarnessExecutionEngine)
    engine.model = "gemini-4-hydrogen"
    engine.fallback_models = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    engine.max_retries = 1
    engine.max_failures = 3
    engine.max_output_tokens = 1024
    engine.failure_count = 0
    engine.locked = False

    if hasattr(engine, "_state_lock"):
        import threading
        engine._state_lock = threading.Lock()

    for _ in range(3):
        engine.run_with_verification("test", always_bad, verify)

    assert engine.failure_count == 3
    assert engine.locked is True

    engine.reset()
    assert engine.locked is False
    assert engine.failure_count == 0
