import json
import os
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_MODEL = "gemini-4-hydrogen"
DEFAULT_FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]


def extract_json_payload(text: str) -> str:
    """Extract JSON content, stripping markdown code fences if wrapped."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1])
        else:
            cleaned = "\n".join(lines[1:])
    return cleaned.strip()


def make_verify_fn(
    min_chars: int = 1,
    required_phrase: str = "",
    require_json: bool = False,
) -> Callable[[str], Tuple[bool, str]]:
    """Build a verifier for output length, required phrase, and optional JSON."""
    phrase = (required_phrase or "").strip()
    min_len = max(1, int(min_chars))

    def _verify(output: str) -> Tuple[bool, str]:
        text = (output or "").strip()
        if not text:
            return False, "Output is empty."
        if len(text) < min_len:
            return (
                False,
                f"Output too short ({len(text)} chars; minimum required is {min_len}).",
            )
        if phrase and phrase.lower() not in text.lower():
            return False, f"Output must include the required phrase: '{phrase}'."
        if require_json:
            payload = extract_json_payload(text)
            try:
                json.loads(payload)
            except json.JSONDecodeError as exc:
                return False, f"Output must be valid JSON ({exc.msg})."
        return True, "All verification checks passed."

    return _verify


def normalize_model_name(model: Optional[str]) -> str:
    """Normalize a model identifier and fall back to DEFAULT_MODEL."""
    if not isinstance(model, str):
        return DEFAULT_MODEL
    cleaned = model.strip()
    if not cleaned:
        return DEFAULT_MODEL
    if "/" in cleaned:
        cleaned = cleaned.split("/")[-1].strip()
    return cleaned or DEFAULT_MODEL


def load_submission_config(config_path: str = "submission.yaml") -> Dict[str, Any]:
    """Load YAML or JSON config file safely, returning an empty dict on failure."""
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}

    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        try:
            loaded = json.loads(raw)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    """Safely parse positive integers while rejecting booleans."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


class HarnessExecutionEngine:
    """Executes prompts with verification retries, model rotation, and circuit breaking."""

    def __init__(
        self,
        model: Optional[str] = None,
        config_path: str = "submission.yaml",
        max_retries: Optional[int] = None,
        max_failures: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        fallback_models: Optional[List[str]] = None,
    ) -> None:
        cfg = load_submission_config(config_path)
        harness_cfg = cfg.get("harness") if isinstance(cfg.get("harness"), dict) else {}

        raw_model = model if model is not None else cfg.get("model", DEFAULT_MODEL)
        self.model = normalize_model_name(raw_model)

        raw_fallbacks = (
            fallback_models
            if fallback_models is not None
            else harness_cfg.get("fallback_models", DEFAULT_FALLBACK_MODELS)
        )
        normalized_fallbacks: List[str] = []
        if isinstance(raw_fallbacks, list):
            for candidate in raw_fallbacks:
                norm = normalize_model_name(candidate)
                if norm != self.model and norm not in normalized_fallbacks:
                    normalized_fallbacks.append(norm)
        self.fallback_models = normalized_fallbacks

        self.max_retries = _safe_int(
            max_retries if max_retries is not None else harness_cfg.get("max_retries"),
            default=3,
            minimum=1,
        )
        self.max_failures = _safe_int(
            max_failures if max_failures is not None else harness_cfg.get("max_failures"),
            default=3,
            minimum=1,
        )
        self.max_output_tokens = _safe_int(
            max_output_tokens
            if max_output_tokens is not None
            else harness_cfg.get("max_output_tokens"),
            default=4096,
            minimum=64,
        )

        self.failure_count = 0
        self.locked = False

    def reset(self) -> Dict[str, Any]:
        """Reset circuit-breaker state and failure counter."""
        self.failure_count = 0
        self.locked = False
        return {
            "status": "reset",
            "locked": self.locked,
            "failure_count": self.failure_count,
            "model": self.model,
        }

    def _model_for_attempt(self, attempt_index: int) -> str:
        """Select primary or fallback model for a 0-based attempt index."""
        if attempt_index == 0 or not self.fallback_models:
            return self.model
        return self.fallback_models[(attempt_index - 1) % len(self.fallback_models)]

    def run_with_verification(
        self,
        prompt: str,
        generate_fn: Callable[[str, str, int], str],
        verify_fn: Callable[[str], Tuple[bool, str]],
    ) -> Dict[str, Any]:
        """Run prompt with retries and model rotation until verified or circuit locks."""
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return {
                "ok": False,
                "status": "invalid_prompt",
                "error": "Prompt cannot be empty.",
                "history": [],
            }

        if self.locked:
            return {
                "ok": False,
                "status": "locked",
                "error": "Circuit breaker is locked. Reset required before running.",
                "failure_count": self.failure_count,
                "history": [],
            }

        history: List[Dict[str, Any]] = []
        last_output = ""
        last_reason = ""
        active_prompt = clean_prompt

        for idx in range(self.max_retries):
            attempt_num = idx + 1
            model_name = self._model_for_attempt(idx)
            try:
                output = generate_fn(active_prompt, model_name, self.max_output_tokens)
                last_output = output or ""
                ok, reason = verify_fn(last_output)
                history.append(
                    {
                        "attempt": attempt_num,
                        "model": model_name,
                        "ok": bool(ok),
                        "reason": reason,
                    }
                )
                if ok:
                    self.failure_count = 0
                    return {
                        "ok": True,
                        "status": "verified",
                        "attempt": attempt_num,
                        "model": model_name,
                        "output": last_output,
                        "reason": reason,
                        "history": history,
                    }
                last_reason = reason
                active_prompt = (
                    f"{clean_prompt}\n\n"
                    f"Previous attempt ({attempt_num}) failed verification: {reason}. "
                    "Please revise and satisfy all requirements."
                )
            except Exception as exc:
                last_reason = str(exc) or exc.__class__.__name__
                history.append(
                    {
                        "attempt": attempt_num,
                        "model": model_name,
                        "ok": False,
                        "reason": last_reason,
                    }
                )
                active_prompt = (
                    f"{clean_prompt}\n\n"
                    f"Previous attempt ({attempt_num}) raised an error: {last_reason}. "
                    "Please try again cleanly."
                )

        self.failure_count += 1
        if self.failure_count >= self.max_failures:
            self.locked = True
            status = "locked"
        else:
            status = "verification_retries_exhausted"

        return {
            "ok": False,
            "status": status,
            "error": last_reason or "Verification failed.",
            "last_reason": last_reason,
            "output": last_output,
            "failure_count": self.failure_count,
            "locked": self.locked,
            "history": history,
        }


def _make_engine(
    tmp_dir: str, max_retries: int = 3, max_failures: int = 3
) -> HarnessExecutionEngine:
    config_path = os.path.join(tmp_dir, "submission.yaml")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(
            "model: gemini-4-hydrogen\n"
            "harness:\n"
            "  fallback_models:\n"
            "    - gemini-3.8-flash\n"
            "    - gemini-3.5-flash-lite\n"
            f"  max_retries: {max_retries}\n"
            f"  max_failures: {max_failures}\n"
            "  max_output_tokens: 4096\n"
        )
    return HarnessExecutionEngine(config_path=config_path)


class TestVerificationAndHarness(unittest.TestCase):
    def test_extract_json_payload_strips_fenced_block(self):
        raw = '```json\n{"status": "ok", "bus_kv": 1.0}\n```'
        parsed = json.loads(extract_json_payload(raw))
        self.assertEqual(parsed, {"status": "ok", "bus_kv": 1.0})

    def test_verify_rejects_malformed_json_even_with_required_phrase(self):
        verify = make_verify_fn(
            min_chars=10,
            required_phrase="microgrid verified",
            require_json=True,
        )
        ok, reason = verify("microgrid verified and operational {bad json")
        self.assertFalse(ok)
        self.assertIn("json", reason.lower())

    def test_verify_min_chars_exact_boundary(self):
        payload = '{"status":"ok","note":"microgrid verified and operational"}'
        verify_exact = make_verify_fn(
            min_chars=len(payload), required_phrase="", require_json=True
        )
        verify_over = make_verify_fn(
            min_chars=len(payload) + 1, required_phrase="", require_json=True
        )
        self.assertTrue(verify_exact(payload)[0])
        self.assertFalse(verify_over(payload)[0])

    def test_verify_phrase_case_insensitive_and_missing(self):
        verify = make_verify_fn(
            min_chars=1, required_phrase="Microgrid Verified", require_json=False
        )
        self.assertTrue(verify("prefix microgrid verified suffix")[0])
        self.assertFalse(verify("missing the target text")[0])

    def test_retry_prompt_carries_failure_reason_and_rotates_models(self):
        seen_calls = []

        def fake_generate(prompt: str, model: str, max_output_tokens: int) -> str:
            seen_calls.append((model, prompt, max_output_tokens))
            if len(seen_calls) == 1:
                return '{"status": "ok"}'
            return (
                '{"status": "ok", "detail": "padded response that easily exceeds '
                'the minimum length requirement"}'
            )

        verify = make_verify_fn(min_chars=120, required_phrase="", require_json=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = _make_engine(tmp_dir, max_retries=3, max_failures=3)
            result = engine.run_with_verification("return json", fake_generate, verify)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(seen_calls), 2)
        self.assertEqual(seen_calls[0][0], "gemini-4-hydrogen")
        self.assertEqual(seen_calls[1][0], "gemini-3.8-flash")
        self.assertIn("minimum required is 120", seen_calls[1][1].lower())

    def test_circuit_breaker_locks_after_max_failures_and_resets_cleanly(self):
        def always_bad(prompt: str, model: str, max_output_tokens: int) -> str:
            return "not json"

        verify = make_verify_fn(
            min_chars=1,
            required_phrase="impossible_phrase_xyz",
            require_json=True,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = _make_engine(tmp_dir, max_retries=1, max_failures=3)
            for _ in range(3):
                engine.run_with_verification("test", always_bad, verify)
            self.assertEqual(engine.failure_count, 3)
            self.assertTrue(engine.locked)

            engine.reset()
            self.assertFalse(engine.locked)
            self.assertEqual(engine.failure_count, 0)


if __name__ == "__main__":
    unittest.main()
