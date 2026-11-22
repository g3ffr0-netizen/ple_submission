"""Shared verification helpers re-exported from harness.py."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import extract_json_payload, make_verify_fn

__all__ = ["extract_json_payload", "make_verify_fn"]
