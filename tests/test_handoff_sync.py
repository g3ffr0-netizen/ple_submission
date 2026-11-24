"""Unit tests for tools/handoff_sync.py two-way handoff parser and updater."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import handoff_sync  # noqa: E402


SAMPLE_DOC = """# Antigravity Two-Way Handoff Mirror

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


class HandoffSyncTests(unittest.TestCase):
    def test_parse_pending_ignores_completed_and_section3(self) -> None:
        pending = handoff_sync.parse_pending_tasks(SAMPLE_DOC)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["task_id"], "TASK-001")
        self.assertEqual(pending[0]["status"], "PENDING")
        self.assertIn("Compare Run #31 vs #30", pending[0]["title"])
        self.assertIn("tuneup compare", pending[0]["body"])

    def test_mark_task_status_and_append_result(self) -> None:
        updated = handoff_sync.mark_task_status(
            SAMPLE_DOC,
            "TASK-001",
            "COMPLETED",
            completed_at="2026-11-24 22:00:00 UTC",
        )
        self.assertEqual(handoff_sync.parse_pending_tasks(updated), [])
        self.assertIn("STATUS: COMPLETED | COMPLETED_AT: 2026-11-24 22:00:00 UTC", updated)
        # Ensure TASK-000 was untouched
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
        # Section 3 still keeps prior output before new entry
        self.assertLess(
            appended.index("[TASK-000] Prior Output"),
            appended.index("[TASK-001] Run #31 Scoreboard"),
        )

    def test_cli_list_pending_and_complete_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "HANDOFF.md"
            p.write_text(SAMPLE_DOC, encoding="utf-8")

            rc_list = handoff_sync.main(["list-pending", "--file", str(p)])
            self.assertEqual(rc_list, 0)

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
