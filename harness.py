from pathlib import Path
from typing import Any, Callable, Dict, Optional
import yaml


def load_submission_config(config_path: str = "submission.yaml") -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_model_name(raw_model: Optional[str], fallback: str = "gemini-3.8-flash") -> str:
    if not raw_model:
        return fallback
    # Strip optional provider prefix like "google-cloud/" or "models/"
    cleaned = raw_model.split("/")[-1].strip()
    return cleaned or fallback


class HarnessExecutionEngine:
    def __init__(
        self,
        model: Optional[str] = None,
        config_path: str = "submission.yaml",
        max_retries: Optional[int] = None,
        max_failures: Optional[int] = None,
    ):
        cfg = load_submission_config(config_path)
        harness_cfg = cfg.get("harness", {})

        resolved_model = model if model is not None else cfg.get("model")
        self.model = normalize_model_name(resolved_model, fallback="gemini-3.8-flash")
        self.max_retries = max_retries if max_retries is not None else int(harness_cfg.get("max_retries", 3))
        self.max_failures = max_failures if max_failures is not None else int(harness_cfg.get("max_failures", 3))
        self.max_output_tokens = int(harness_cfg.get("max_output_tokens", 4096))

        self.failure_count = 0
        self.locked = False

    def reset(self) -> Dict[str, Any]:
        self.failure_count = 0
        self.locked = False
        return {"status": "reset", "locked": self.locked, "failure_count": self.failure_count}

    def run_with_verification(
        self,
        prompt: str,
        generate_fn: Callable[[str, str, int], str],
        verify_fn: Callable[[str], tuple[bool, str]],
    ) -> Dict[str, Any]:
        if self.locked:
            return {
                "ok": False,
                "status": "locked",
                "error": "Harness is locked due to repeated failures. Call reset() to unlock.",
                "failure_count": self.failure_count,
                "locked": True,
            }

        active_prompt = prompt
        last_reason = ""
        last_output = ""

        for attempt in range(1, self.max_retries + 1):
            last_output = generate_fn(active_prompt, self.model, self.max_output_tokens)
            passed, reason = verify_fn(last_output)
            if passed:
                return {
                    "ok": True,
                    "status": "verified",
                    "attempt": attempt,
                    "model": self.model,
                    "output": last_output,
                    "locked": self.locked,
                    "failure_count": self.failure_count,
                }

            last_reason = reason or "Verification check failed."
            active_prompt = (
                f"{prompt}\n\n"
                f"[Verification Feedback — Attempt {attempt}/{self.max_retries}]:\n"
                f"Your previous response did not pass verification because: {last_reason}\n"
                f"Revise your response so every hard constraint, calculation, and formatting rule is satisfied completely."
            )

        self.failure_count += 1
        if self.failure_count >= self.max_failures:
            self.locked = True
            return {
                "ok": False,
                "status": "locked",
                "error": f"Verification retries exhausted; harness locked after {self.failure_count} failures.",
                "last_reason": last_reason,
                "output": last_output,
                "locked": True,
                "failure_count": self.failure_count,
            }

        return {
            "ok": False,
            "status": "verification_retries_exhausted",
            "error": "Verification retries exhausted.",
            "last_reason": last_reason,
            "output": last_output,
            "locked": False,
            "failure_count": self.failure_count,
        }
