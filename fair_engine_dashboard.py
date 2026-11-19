import re
from typing import Iterable, List, Tuple


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
    required_terms: Iterable[str] = (
        "current",
        "loss",
        "48 v",
        "400 v",
        "verdict",
    ),
    require_table: bool = True,
    min_chars: int = 120,
) -> Tuple[bool, str]:
    """
    Verify response quality, required sections/topics, table presence,
    and basic numerical consistency before marking a run VERIFIED.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False, "Empty output returned."

    if len(cleaned) < min_chars:
        return False, f"Output too short ({len(cleaned)} chars); provide a fuller breakdown."

    lowered = cleaned.lower()

    # 1. Check required topics / section keywords
    missing_terms = [term for term in required_terms if term.lower() not in lowered]
    if missing_terms:
        return False, f"Missing required sections/terms: {', '.join(missing_terms)}."

    # 2. Check for a summary table if requested
    if require_table and not _has_markdown_table(cleaned):
        return False, "Missing a Markdown summary comparison table."

    # 3. Optional numerical consistency checks for 48V vs 400V DC / 10 kW examples
    # Check 48V current around ~200-235 A if mentioned alongside 48 V
    if "48 v" in lowered or "48v" in lowered:
        amps_48v = _extract_floats(r"48\s*v[^0-9\n]{0,80}?(\d+(?:\.\d+)?)\s*a\b", cleaned)
        # Also catch "208.3 A ... 48 V" order
        amps_48v += _extract_floats(r"\b(\d+(?:\.\d+)?)\s*a\b[^0-9\n]{0,40}?48\s*v", cleaned)
        for val in amps_48v:
            if not (180.0 <= val <= 250.0):
                return False, f"Implausible 48 V current value ({val} A) for a 10 kW system."

    # Check 400V current around ~20-30 A if mentioned alongside 400 V
    if "400 v" in lowered or "400v" in lowered:
        amps_400v = _extract_floats(r"400\s*v[^0-9\n]{0,80}?(\d+(?:\.\d+)?)\s*a\b", cleaned)
        amps_400v += _extract_floats(r"\b(\d+(?:\.\d+)?)\s*a\b[^0-9\n]{0,40}?400\s*v", cleaned)
        for val in amps_400v:
            if not (15.0 <= val <= 40.0):
                return False, f"Implausible 400 V current value ({val} A) for a 10 kW system."

    # Check I^2 * R scaling factor (~69x to 70x for 400V vs 48V) if mentioned
    scaling_factors = _extract_floats(r"(\d+(?:\.\d+)?)\s*[×x]\s*(?:higher|more|loss|scaling|current)", cleaned)
    for factor in scaling_factors:
        # Exclude small factors like 1x or 2x; if near ~69-70, ensure it's in a sane range
        if 50.0 <= factor <= 90.0 and not (65.0 <= factor <= 75.0):
            return False, f"Unexpected I^2*R scaling factor ({factor}x); expected ~69.4x."

    return True, "Verified"
