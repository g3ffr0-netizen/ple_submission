import json
from typing import Tuple


def extract_json_payload(text: str) -> str:
    """Extract JSON content, stripping markdown code fences if wrapped."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(
            lines[1:-1]
            if len(lines) >= 3 and lines[-1].strip() == "```"
            else lines[1:]
        ).strip()
    return cleaned


def make_verify_fn(min_chars: int, required_phrase: str, require_json: bool):
    """Build a verifier for the optional output constraints in the portal."""
    phrase = required_phrase.strip()

    def _verify(output: str) -> Tuple[bool, str]:
        text = (output or "").strip()
        if not text:
            return False, "Output is empty."
        if len(text) < min_chars:
            return (
                False,
                f"Output too short ({len(text)} chars; minimum required is {min_chars}).",
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
