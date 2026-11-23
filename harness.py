"""Core execution harness with verification retries, fallback rotation, and circuit breaking."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODELS = [
    "gemini-4-argon",
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
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


def _safe_nonneg_float(value: Any, default: float = 0.0) -> float:
    """Safely parse non-negative finite floats while rejecting booleans."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(0.0, parsed)


def _extract_retry_after(exc: Exception) -> Optional[float]:
    """Extract numeric Retry-After seconds from an exception or its response headers."""
    candidates: List[Any] = [getattr(exc, "retry_after", None)]
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                candidates.append(v)
    elif hasattr(headers, "get"):
        candidates.append(headers.get("retry-after") or headers.get("Retry-After"))
    for raw in candidates:
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val) and val > 0.0:
            return val
    return None


def classify_transient_error(exc: Exception) -> Tuple[str, Optional[float]]:
    """Classify an API/model exception for backoff handling and extract Retry-After."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status_int = int(status) if status is not None and not isinstance(status, bool) else None
    except (TypeError, ValueError):
        status_int = None

    err_l = str(exc).lower()
    retry_after = _extract_retry_after(exc)
    if status_int == 429 or any(
        tok in err_l
        for tok in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted", "too many requests")
    ):
        return "rate_limit", retry_after
    if (status_int is not None and status_int >= 500) or any(
        tok in err_l
        for tok in (
            "500",
            "502",
            "503",
            "504",
            "overloaded",
            "service unavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
        )
    ):
        return "server_error", retry_after
    if status_int in (408, 409) or any(
        tok in err_l
        for tok in ("timeout", "timed out", "deadline exceeded", "connection", "temporarily")
    ):
        return "transient", retry_after
    return "error", retry_after


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


def is_pass_verdict(verdict: Any) -> bool:
    """Return True if a verifier reply unambiguously indicates PASS."""
    parsed_dict: Optional[Dict[str, Any]] = None
    if isinstance(verdict, dict):
        parsed_dict = verdict
    elif isinstance(verdict, str):
        stripped = verdict.strip("`*_.,!:; \r\n\t")
        if stripped.upper() == "PASS":
            return True
        payload = extract_json_payload(verdict)
        if payload:
            try:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    parsed_dict = decoded
            except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
                return False
    else:
        return False

    if parsed_dict is None:
        return False

    for key in ("pass", "ok", "passed"):
        if isinstance(parsed_dict.get(key), bool):
            return bool(parsed_dict[key])
    for key in ("verdict", "status", "result"):
        val = parsed_dict.get(key)
        if isinstance(val, str) and val.strip("`*_.,!:; \r\n\t").upper() in (
            "PASS",
            "PASSED",
            "OK",
        ):
            return True
    return False


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


class CircuitBreaker:
    """Tracks consecutive failures and locks execution past `max_failures`."""

    def __init__(self, max_failures: int = 3) -> None:
        self.max_failures = _safe_int(max_failures, default=3, minimum=1)
        self.failure_count = 0
        self.locked = False

    def record_success(self) -> None:
        """Reset consecutive failure streak on a verified/clean success."""
        self.failure_count = 0

    def record_failure(self) -> bool:
        """Increment failure count and lock once max_failures is reached."""
        self.failure_count += 1
        if self.failure_count >= self.max_failures:
            self.locked = True
        return self.locked

    def reset(self) -> Dict[str, Any]:
        """Reset consecutive failure count and clear lockout state."""
        self.failure_count = 0
        self.locked = False
        return {
            "status": "reset",
            "locked": self.locked,
            "failure_count": self.failure_count,
        }


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
        retry_backoff_sec: Optional[float] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = load_submission_config(config_path)
        harness_cfg = cfg.get("harness") if isinstance(cfg.get("harness"), dict) else {}
        env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}

        if model is not None:
            raw_model: Any = model
        elif "model_api" in cfg:
            raw_model = cfg.get("model_api")
        else:
            raw_model = cfg.get("model", DEFAULT_MODEL)
        self.model = normalize_model_name(raw_model)

        if fallback_models is not None:
            raw_fallbacks: Any = fallback_models
        elif "fallback_models" in harness_cfg:
            raw_fallbacks = harness_cfg.get("fallback_models")
        else:
            raw_fallbacks = DEFAULT_FALLBACK_MODELS

        normalized_fallbacks: List[str] = []
        if isinstance(raw_fallbacks, list):
            for candidate in raw_fallbacks:
                norm = normalize_model_name(candidate)
                if norm != self.model and norm not in normalized_fallbacks:
                    normalized_fallbacks.append(norm)
        self.fallback_models = normalized_fallbacks

        raw_retries = (
            max_retries
            if max_retries is not None
            else (
                harness_cfg.get("max_retries")
                if "max_retries" in harness_cfg
                else env_cfg.get("MAX_RETRIES")
            )
        )
        raw_failures = (
            max_failures
            if max_failures is not None
            else (
                harness_cfg.get("max_failures")
                if "max_failures" in harness_cfg
                else env_cfg.get("MAX_FAILURES")
            )
        )
        raw_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else (
                harness_cfg.get("max_output_tokens")
                if "max_output_tokens" in harness_cfg
                else env_cfg.get("MAX_OUTPUT_TOKENS")
            )
        )

        self.max_retries = _safe_int(raw_retries, default=3, minimum=1)
        self._breaker = CircuitBreaker(
            max_failures=_safe_int(raw_failures, default=3, minimum=1)
        )
        self.max_output_tokens = _safe_int(
            raw_output_tokens, default=4096, minimum=64
        )
        raw_backoff = (
            retry_backoff_sec
            if retry_backoff_sec is not None
            else (
                harness_cfg.get("retry_backoff_sec")
                if "retry_backoff_sec" in harness_cfg
                else env_cfg.get("RETRY_BACKOFF_SEC")
            )
        )
        self.retry_backoff_sec = _safe_nonneg_float(raw_backoff, default=0.0)
        self._sleep_fn = sleep_fn

    @property
    def max_failures(self) -> int:
        return self._breaker.max_failures

    @max_failures.setter
    def max_failures(self, value: int) -> None:
        self._breaker.max_failures = _safe_int(value, default=3, minimum=1)

    @property
    def failure_count(self) -> int:
        return self._breaker.failure_count

    @failure_count.setter
    def failure_count(self, value: int) -> None:
        self._breaker.failure_count = int(value)

    @property
    def locked(self) -> bool:
        return self._breaker.locked

    @locked.setter
    def locked(self, value: bool) -> None:
        self._breaker.locked = bool(value)

    def reset(self) -> Dict[str, Any]:
        """Reset circuit-breaker state and failure counter."""
        state = self._breaker.reset()
        logger.info("Harness circuit breaker reset (model=%s).", self.model)
        return {
            **state,
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
            logger.warning(
                "Harness execution blocked: circuit breaker is locked (failures=%d).",
                self.failure_count,
            )
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
        transient_streak = 0

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
                    self._breaker.record_success()
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
                transient_streak = 0
                last_reason = reason
                logger.warning(
                    "Attempt %d/%d failed verification on model=%s: %s",
                    attempt_num,
                    self.max_retries,
                    model_name,
                    reason,
                )
                active_prompt = (
                    f"{clean_prompt}\n\n"
                    f"Previous attempt ({attempt_num}) failed verification: {reason}. "
                    "Please revise and satisfy all requirements."
                )
            except Exception as exc:
                last_reason = str(exc) or exc.__class__.__name__
                err_kind, retry_after = classify_transient_error(exc)
                if err_kind != "error":
                    delay = min(
                        30.0,
                        max(
                            retry_after or 0.0,
                            self.retry_backoff_sec * (2**transient_streak),
                        ),
                    )
                    transient_streak += 1
                else:
                    delay = 0.0
                    transient_streak = 0
                logger.warning(
                    "Attempt %d/%d raised %s on model=%s: %s",
                    attempt_num,
                    self.max_retries,
                    err_kind,
                    model_name,
                    last_reason,
                )
                history.append(
                    {
                        "attempt": attempt_num,
                        "model": model_name,
                        "ok": False,
                        "reason": last_reason,
                        "error_kind": err_kind,
                        "backoff_sec": round(delay, 3),
                    }
                )
                if delay > 0.0 and attempt_num < self.max_retries:
                    self._sleep_fn(delay)
                active_prompt = (
                    f"{clean_prompt}\n\n"
                    f"Previous attempt ({attempt_num}) raised an error: {last_reason}. "
                    "Please try again cleanly."
                )

        if self._breaker.record_failure():
            status = "locked"
            logger.warning(
                "Circuit breaker locked after %d consecutive failures.",
                self.failure_count,
            )
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
