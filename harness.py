import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_MODEL = "gemini-4-hydrogen"
DEFAULT_FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]

_CLOSED_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    """Safely parse positive integers while rejecting booleans."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def extract_json_payload(text: Any) -> str:
    """Extract JSON content, stripping prose and markdown code fences if wrapped."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif not isinstance(text, str):
        return ""

    cleaned = text.strip()
    if not cleaned:
        return ""

    closed_blocks = _CLOSED_FENCE_RE.findall(cleaned)
    if closed_blocks:
        prioritized = sorted(
            closed_blocks,
            key=lambda item: 0 if item[0].strip().lower() == "json" else 1,
        )
        candidates = [body.strip() for _, body in prioritized]
        for candidate in candidates:
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, RecursionError):
                continue
        return candidates[0]

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        return "\n".join(lines[1:]).strip()

    return cleaned


def make_verify_fn(
    min_chars: int = 1,
    required_phrase: str = "",
    require_json: bool = False,
) -> Callable[[Any], Tuple[bool, str]]:
    """Build a verifier for output length, required phrase, and optional JSON."""
    phrase = required_phrase.strip() if isinstance(required_phrase, str) else ""
    min_len = _safe_int(min_chars, default=1, minimum=1)

    def _verify(output: Any) -> Tuple[bool, str]:
        if output is None:
            return False, "Output is empty."
        if not isinstance(output, str):
            return False, f"Output must be a string (got {type(output).__name__})."
        text = output.strip()
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
            except RecursionError:
                return False, "Output JSON exceeds maximum nesting depth."
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
        started = time.perf_counter()

        def _elapsed_ms() -> float:
            return round((time.perf_counter() - started) * 1000.0, 2)

        if not isinstance(prompt, str) or not prompt.strip():
            return {
                "ok": False,
                "status": "invalid_prompt",
                "error": "Prompt must be a non-empty string.",
                "elapsed_ms": _elapsed_ms(),
                "history": [],
            }
        clean_prompt = prompt.strip()

        if self.locked:
            return {
                "ok": False,
                "status": "locked",
                "locked": True,
                "error": "Circuit breaker is locked. Reset required before running.",
                "failure_count": self.failure_count,
                "elapsed_ms": _elapsed_ms(),
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
                last_output = output if isinstance(output, str) else ""
                verify_input = "" if output is None else output
                ok, reason = verify_fn(verify_input)
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
                        "elapsed_ms": _elapsed_ms(),
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
            "elapsed_ms": _elapsed_ms(),
            "history": history,
        }

