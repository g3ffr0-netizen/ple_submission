"""Unit tests for tools/handoff_sync.py two-way handoff parser, mirror updater, and gdocs structure parsing."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import handoff_sync  # noqa: E402


SAMPLE_MIRROR_DOC = """# Antigravity Two-Way Handoff Mirror

## Section 1: Active Status & Context
Running Run #31.

## Section 2: Gemini -> Antigravity

### [TASK-001] Compare Run #31 vs #30
STATUS: PENDING
Run tuneup compare once Run #31 finishes.

### [TASK-000] Prior Task
STATUS: COMPLETED | COMPLETED_AT: 2026-11-24 10:00:00 UTC
Already finished.

## Section 3: Antigravity -> Gemini

### [2026-11-24 10:05:00 UTC] [TASK-000] Prior Output

All checks passed.
"""

SAMPLE_LIVE_GDOCS_MD = r"""# Antigravity <-> Gemini Handoff

## 1. Active Tracks
Ok.

## 2. Gemini -> Antigravity (Task Queue)

Antigravity polls this section for tasks marked **PENDING**.

\[TASK-001\] STATUS: PENDING ACTION: 1. Once PLE Dev Eval Run \#31 (45477741941) finishes, run `py -3.14 tools/tuneup.py compare` against Run \#30.

## 3. Antigravity -> Gemini (Results & Logs)

\[LOG-INIT\] 2026-11-25: Handoff document initialized.

## 4. Sync Protocol

No Overwrites.
"""

SAMPLE_STRUCTURE = """
  4  HEADING_2         "1. Active Tracks"
  7  HEADING_2         "2. Gemini -> Antigravity (Task Queue)"
  8  NORMAL_TEXT      "Antigravity polls this section..."
  9  NORMAL_TEXT      "[TASK-001]\\vSTATUS: PENDING\\vACTION:..."
 10  NORMAL_TEXT      "\\u00a0"
 11  HEADING_2         "3. Antigravity -> Gemini (Results & Logs)"
 12  NORMAL_TEXT      "Antigravity appends completed outputs..."
 13  NORMAL_TEXT      "[LOG-INIT]\\v2026-11-25: Handoff document initialized."
 14  NORMAL_TEXT      "\\u00a0"
 15  HEADING_2         "4. Sync Protocol"
"""


class HandoffSyncTests(unittest.TestCase):
    def test_parse_pending_ignores_completed_and_section3(self) -> None:
        pending = handoff_sync.parse_pending_tasks(SAMPLE_MIRROR_DOC)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task_id"], "TASK-001")
        self.assertEqual(pending[0]["status"], "PENDING")
        self.assertIn("Compare Run #31 vs #30", pending[0]["title"])
        self.assertIn("tuneup compare", pending[0]["body"])

    def test_parse_and_update_live_gdocs_markdown_format(self) -> None:
        pending = handoff_sync.parse_pending_tasks(SAMPLE_LIVE_GDOCS_MD)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task_id"], "TASK-001")
        self.assertIn("45477741941", pending[0]["body"])

        updated = handoff_sync.mark_task_status(SAMPLE_LIVE_GDOCS_MD, "TASK-001", "IN_PROGRESS")
        self.assertEqual(handoff_sync.parse_pending_tasks(updated), [])
        self.assertIn("STATUS: IN_PROGRESS", updated)

    def test_parse_structure_finds_task_and_section3_anchor(self) -> None:
        parsed = handoff_sync.parse_gdocs_structure(SAMPLE_STRUCTURE)
        self.assertEqual(handoff_sync.gdocs_find_task_elem(parsed, "TASK-001"), 9)
        self.assertEqual(handoff_sync.gdocs_find_section3_insert_anchor(parsed), 13)

    def test_mark_task_status_and_append_result(self) -> None:
        updated = handoff_sync.mark_task_status(
            SAMPLE_MIRROR_DOC,
            "TASK-001",
            "COMPLETED",
            completed_at="2026-11-24 22:00:00 UTC",
        )
        self.assertEqual(handoff_sync.parse_pending_tasks(updated), [])
        self.assertIn("STATUS: COMPLETED | COMPLETED_AT: 2026-11-24 22:00:00 UTC", updated)
        self.assertIn("COMPLETED_AT: 2026-11-24 10:00:00 UTC", updated)

        appended = handoff_sync.append_result(
            updated,
            "TASK-001",
            "Run #31 Scoreboard",
            "GATE=PROMOTE\nMean score: 0.4500",
            timestamp="2026-11-24 22:00:00 UTC",
        )
        self.assertIn("### [2026-11-24 22:00:00 UTC] [TASK-001] Run #31 Scoreboard", appended)
        self.assertIn("GATE=PROMOTE", appended)
        self.assertLess(
            appended.index("[TASK-000] Prior Output"),
            appended.index("[TASK-001] Run #31 Scoreboard"),
        )

    def test_cli_list_pending_and_complete_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "HANDOFF.md"
            p.write_text(SAMPLE_MIRROR_DOC, encoding="utf-8")

            self.assertEqual(handoff_sync.main(["list-pending", "--file", str(p)]), 0)
            rc_done = handoff_sync.main(
                [
                    "complete",
                    "--file",
                    str(p),
                    "--task-id",
                    "TASK-001",
                    "--title",
                    "Run #31 Done",
                    "--body",
                    "Scoreboard verified.",
                ]
            )
            self.assertEqual(rc_done, 0)
            final_text = p.read_text(encoding="utf-8")
            self.assertEqual(handoff_sync.parse_pending_tasks(final_text), [])
            self.assertIn("[TASK-001] Run #31 Done", final_text)
            self.assertIn("Scoreboard verified.", final_text)


if __name__ == "__main__":
    unittest.main()
