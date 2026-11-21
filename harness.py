from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


DEFAULT_MODEL = "gemini-3.8-flash"

GenerateFn = Callable[[str, str, int], str]
VerifyFn = Callable[[str], Tuple[bool, str]]


def load_submission_config(config_path: str = "submission.yaml") -> Dict[str, Any]:
    """Safely load submission.yaml, returning an empty dict if missing, invalid, or PyYAML is unavailable."""
    if not config_path or yaml is None:
        return {}
    path = Path(config_path)
    if not path.exists() or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, Exception):
        return {}


def normalize_model_name(raw_model: Optional[str], fallback: str = DEFAULT_MODEL) -> str:
    """Normalize a model identifier by stripping optional provider prefixes."""
    if not raw_model or not isinstance(raw_model, str):
        return fallback
    cleaned = raw_model.split("/")[-1].strip()
    return cleaned or fallback


def _safe_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
        return max(minimum, parsed)
    except (TypeError, ValueError):
        return default


def _truncate_snippet(text: str, limit: int = 800) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}\n...[truncated]"


@dataclass
class AttemptRecord:
    attempt: int
    model: str
    passed: bool
    reason: str
    elapsed_ms: float
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "model": self.model,
            "passed": self.passed,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "error": self.error,
        }


class HarnessExecutionEngine:
    """Self-correcting verification harness with circuit-breaker protection and full attempt telemetry."""

    def __init__(
        self,
        model: Optional[str] = None,
        config_path: str = "submission.yaml",
        max_retries: Optional[int] = None,
        max_failures: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        fallback_models: Optional[List[str]] = None,
        reset_failures_on_success: bool = True,
    ):
        cfg = load_submission_config(config_path)
        harness_cfg = cfg.get("harness")
        if not isinstance(harness_cfg, dict):
            harness_cfg = {}

        resolved_model = model if model is not None else cfg.get("model")
        self.model = normalize_model_name(resolved_model, fallback=DEFAULT_MODEL)

        cfg_fallbacks = harness_cfg.get("fallback_models", [])
        raw_fallbacks = (
            fallback_models
            if fallback_models is not None
            else (cfg_fallbacks if isinstance(cfg_fallbacks, list) else [])
        )
        seen = {self.model}
        cleaned_fallbacks: List[str] = []
        for item in raw_fallbacks:
            if isinstance(item, str) and item.strip():
                norm = normalize_model_name(item, fallback=self.model)
                if norm not in seen:
                    seen.add(norm)
                    cleaned_fallbacks.append(norm)
        self.fallback_models = cleaned_fallbacks

        self.max_retries = _safe_positive_int(
            max_retries if max_retries is not None else harness_cfg.get("max_retries", 3),
            default=3,
        )
        self.max_failures = _safe_positive_int(
            max_failures if max_failures is not None else harness_cfg.get("max_failures", 3),
            default=3,
        )
        self.max_output_tokens = _safe_positive_int(
            max_output_tokens
            if max_output_tokens is not None
            else harness_cfg.get("max_output_tokens", 4096),
            default=4096,
            minimum=64,
        )
        self.reset_failures_on_success = bool(reset_failures_on_success)

        self._state_lock = threading.Lock()
        self.failure_count = 0
        self.locked = False

    def reset(self) -> Dict[str, Any]:
        """Unlock the harness and reset the consecutive failure counter."""
        with self._state_lock:
            self.failure_count = 0
            self.locked = False
            return {
                "status": "reset",
                "locked": self.locked,
                "failure_count": self.failure_count,
            }

    def _build_retry_prompt(
        self,
        base_prompt: str,
        feedback_log: List[Tuple[int, str]],
        last_output: str,
    ) -> str:
        """Build a cumulative feedback prompt so the model fixes new issues without regressing on prior ones."""
        history_lines = "\n".join(f"- Attempt {att}: {reason}" for att, reason in feedback_log)
        snippet = _truncate_snippet(last_output, limit=800)

        parts = [
            base_prompt,
            f"[Verification Feedback — {len(feedback_log)}/{self.max_retries} failed]:",
            "Fix ALL of the following issues without regressing on previously satisfied constraints:",
            history_lines,
        ]
        if snippet:
            parts.append(f"[Previous Output Snippet]:\n{snippet}")
        parts.append(
            "Revise your response so every hard constraint, calculation, and formatting rule is satisfied completely."
        )
        return "\n\n".join(parts)

    def _select_model_for_attempt(self, attempt: int) -> str:
        """Use the primary model first, rotating through configured fallback models on later retries if available."""
        if attempt <= 1 or not self.fallback_models:
            return self.model
        idx = (attempt - 2) % len(self.fallback_models)
        return self.fallback_models[idx]

    def _record_failure(
        self,
        history: List[AttemptRecord],
        feedback_log: List[Tuple[int, str]],
        attempt: int,
        model: str,
        elapsed_ms: float,
        reason: str,
        error: Optional[str] = None,
    ) -> str:
        clean_reason = reason.strip() or "Verification check failed."
        feedback_log.append((attempt, clean_reason))
        history.append(
            AttemptRecord(
                attempt=attempt,
                model=model,
                passed=False,
                reason=clean_reason,
                elapsed_ms=elapsed_ms,
                error=error,
            )
        )
        return clean_reason

    def run_with_verification(
        self,
        prompt: str,
        generate_fn: GenerateFn,
        verify_fn: VerifyFn,
    ) -> Dict[str, Any]:
        with self._state_lock:
            if self.locked:
                return {
                    "ok": False,
                    "status": "locked",
                    "error": "Harness is locked due to repeated failures. Call reset() to unlock.",
                    "failure_count": self.failure_count,
                    "locked": True,
                    "elapsed_ms": 0.0,
                    "history": [],
                }
            current_failures = self.failure_count

        if not isinstance(prompt, str) or not prompt.strip():
            return {
                "ok": False,
                "status": "invalid_prompt",
                "error": "Prompt must be a non-empty string.",
                "failure_count": current_failures,
                "locked": False,
                "elapsed_ms": 0.0,
                "history": [],
            }

        run_start = time.perf_counter()
        active_prompt = prompt
        last_reason = ""
        last_output = ""
        feedback_log: List[Tuple[int, str]] = []
        history: List[AttemptRecord] = []

        for attempt in range(1, self.max_retries + 1):
            attempt_start = time.perf_counter()
            active_model = self._select_model_for_attempt(attempt)

            try:
                raw_output = generate_fn(active_prompt, active_model, self.max_output_tokens)
                last_output = "" if raw_output is None else str(raw_output)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - attempt_start) * 1000.0
                err_msg = f"Generation error ({type(exc).__name__}): {exc}"
                last_reason = self._record_failure(
                    history=history,
                    feedback_log=feedback_log,
                    attempt=attempt,
                    model=active_model,
                    elapsed_ms=elapsed_ms,
                    reason=err_msg,
                    error=err_msg,
                )
                active_prompt = self._build_retry_prompt(prompt, feedback_log, last_output)
                continue

            try:
                ver_result = verify_fn(last_output)
                if not isinstance(ver_result, tuple) or len(ver_result) != 2:
                    raise TypeError("verify_fn must return a (bool, str) tuple.")
                passed = bool(ver_result[0])
                reason = str(ver_result[1]) if ver_result[1] is not None else ""
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - attempt_start) * 1000.0
                err_msg = f"Verification error ({type(exc).__name__}): {exc}"
                last_reason = self._record_failure(
                    history=history,
                    feedback_log=feedback_log,
                    attempt=attempt,
                    model=active_model,
                    elapsed_ms=elapsed_ms,
                    reason=err_msg,
                    error=err_msg,
                )
                active_prompt = self._build_retry_prompt(prompt, feedback_log, last_output)
                continue

            elapsed_ms = (time.perf_counter() - attempt_start) * 1000.0
            if passed:
                with self._state_lock:
                    if self.reset_failures_on_success:
                        self.failure_count = 0
                    updated_failures = self.failure_count
                    is_locked = self.locked

                history.append(
                    AttemptRecord(
                        attempt=attempt,
                        model=active_model,
                        passed=True,
                        reason=reason.strip() or "Verified.",
                        elapsed_ms=elapsed_ms,
                    )
                )
                return {
                    "ok": True,
                    "status": "verified",
                    "attempt": attempt,
                    "model": active_model,
                    "output": last_output,
                    "locked": is_locked,
                    "failure_count": updated_failures,
                    "elapsed_ms": round((time.perf_counter() - run_start) * 1000.0, 2),
                    "history": [rec.to_dict() for rec in history],
                }

            last_reason = self._record_failure(
                history=history,
                feedback_log=feedback_log,
                attempt=attempt,
                model=active_model,
                elapsed_ms=elapsed_ms,
                reason=reason or "Verification check failed.",
            )
            active_prompt = self._build_retry_prompt(prompt, feedback_log, last_output)

        with self._state_lock:
            self.failure_count += 1
            updated_failures = self.failure_count
            if updated_failures >= self.max_failures:
                self.locked = True
            is_locked = self.locked

        total_ms = round((time.perf_counter() - run_start) * 1000.0, 2)

        if is_locked:
            return {
                "ok": False,
                "status": "locked",
                "error": f"Verification retries exhausted; harness locked after {updated_failures} failures.",
                "last_reason": last_reason,
                "output": last_output,
                "locked": True,
                "failure_count": updated_failures,
                "elapsed_ms": total_ms,
                "history": [rec.to_dict() for rec in history],
            }

        return {
            "ok": False,
            "status": "verification_retries_exhausted",
            "error": "Verification retries exhausted.",
            "last_reason": last_reason,
            "output": last_output,
            "locked": False,
            "failure_count": updated_failures,
            "elapsed_ms": total_ms,
            "history": [rec.to_dict() for rec in history],
        }
