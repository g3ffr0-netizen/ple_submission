#!/usr/bin/env python3
"""Two-way handoff bridge for `Antigravity_Handoff` via Google Docs (when configured) or GitHub-mirrored `HANDOFF.md`."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HANDOFF_PATH = REPO_ROOT / "HANDOFF.md"
DEFAULT_DOC_ID = "1sYMIHG99QP_92-C9hvm4noj2huuJByfOiyDaECJnxxw"
DEFAULT_DOC_NAME = "Antigravity_Handoff"

SECTION_2_HEADER = "## Section 2: Gemini -> Antigravity"
SECTION_3_HEADER = "## Section 3: Antigravity -> Gemini"

_SECTION_HDR_RE = re.compile(r"^##\s+Section\s+(\d+)\b.*$", re.M)
_TASK_HDR_RE = re.compile(r"^###\s*\[(TASK-\d+)\](.*)$", re.M)
_STATUS_LINE_RE = re.compile(r"^(\s*(?:-\s*)?STATUS:\s*)([A-Za-z0-9_-]+)(.*)$", re.I | re.M)


def utc_now_str() -> str:
    """Return current UTC timestamp formatted cleanly for handoff entries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def google_docs_available() -> bool:
    """Return True if Google Docs Python client packages and local ADC/service credentials are configured."""
    has_client = importlib.util.find_spec("googleapiclient") is not None and (
        importlib.util.find_spec("google.oauth2") is not None
        or importlib.util.find_spec("google_auth_oauthlib") is not None
    )
    if not has_client:
        return False
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        return Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).is_file()
    adc = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    home_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    return adc.is_file() or home_adc.is_file()


def _find_section_bounds(markdown_text: str, section_num: int) -> Optional[Tuple[int, int, int]]:
    """Return (header_start, body_start, section_end) offsets for `## Section <section_num>...`."""
    matches = list(_SECTION_HDR_RE.finditer(markdown_text))
    for idx, m in enumerate(matches):
        if int(m.group(1)) == section_num:
            body_start = markdown_text.find("\n", m.end())
            body_start = len(markdown_text) if body_start == -1 else body_start + 1
            sec_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
            return m.start(), body_start, sec_end
    return None


def _iter_section2_tasks(section2_body: str) -> List[Tuple[str, str, int, int, str]]:
    """Return (task_id, title_suffix, start, end, block_text) for all `[TASK-XXX]` blocks in Section 2."""
    hdrs = list(_TASK_HDR_RE.finditer(section2_body))
    tasks: List[Tuple[str, str, int, int, str]] = []
    for idx, m in enumerate(hdrs):
        end = hdrs[idx + 1].start() if idx + 1 < len(hdrs) else len(section2_body)
        tasks.append((m.group(1).strip(), m.group(2).strip(), m.start(), end, section2_body[m.start():end]))
    return tasks


def parse_pending_tasks(markdown_text: str) -> List[Dict[str, str]]:
    """Extract all `[TASK-XXX]` items in Section 2 whose status is `PENDING`."""
    bounds = _find_section_bounds(markdown_text, 2)
    if bounds is None:
        return []
    _, body_start, sec_end = bounds
    sec2_body = markdown_text[body_start:sec_end]

    pending: List[Dict[str, str]] = []
    for task_id, title_suffix, _, _, block in _iter_section2_tasks(sec2_body):
        sm = _STATUS_LINE_RE.search(block)
        if not sm:
            continue
        status = sm.group(2).strip().upper()
        if status != "PENDING":
            continue
        lines = block.splitlines()[1:]
        body_lines = [ln for ln in lines if not _STATUS_LINE_RE.match(ln)]
        pending.append(
            {
                "task_id": task_id,
                "status": status,
                "title": title_suffix.lstrip("-: ").strip(),
                "body": "\n".join(body_lines).strip(),
            }
        )
    return pending


def mark_task_status(
    markdown_text: str,
    task_id: str,
    new_status: str,
    completed_at: Optional[str] = None,
) -> str:
    """Update the status of `[TASK-XXX]` inside Section 2 without touching other tasks or sections."""
    bounds = _find_section_bounds(markdown_text, 2)
    if bounds is None:
        raise ValueError(f"Could not find {SECTION_2_HEADER!r} in handoff markdown.")
    _, body_start, sec_end = bounds
    sec2_body = markdown_text[body_start:sec_end]
    norm_status = new_status.strip().upper()

    for tid, _, t_start, t_end, block in _iter_section2_tasks(sec2_body):
        if tid != task_id:
            continue
        sm = _STATUS_LINE_RE.search(block)
        if sm:
            tail = sm.group(3)
            if norm_status == "COMPLETED" and completed_at:
                tail = re.sub(r"\s*\|\s*COMPLETED_AT:.*$", "", tail).rstrip()
                tail = f"{tail} | COMPLETED_AT: {completed_at}"
            updated_block = (
                block[: sm.start()]
                + f"{sm.group(1)}{norm_status}{tail}"
                + block[sm.end() :]
            )
        else:
            nl = block.find("\n")
            insert_line = f"\nSTATUS: {norm_status}" + (
                f" | COMPLETED_AT: {completed_at}" if completed_at else ""
            )
            updated_block = (block + insert_line) if nl == -1 else (block[:nl] + insert_line + block[nl:])

        new_sec2_body = sec2_body[:t_start] + updated_block + sec2_body[t_end:]
        return markdown_text[:body_start] + new_sec2_body + markdown_text[sec_end:]

    raise KeyError(f"Task {task_id!r} not found in Section 2.")


def append_result(
    markdown_text: str,
    task_id: str,
    title: str,
    body: str,
    timestamp: Optional[str] = None,
) -> str:
    """Append a timestamped `[<task_id>]` result block to Section 3 (`Antigravity -> Gemini`)."""
    ts = timestamp or utc_now_str()
    clean_title = title.strip()
    heading = f"### [{ts}] [{task_id.strip()}]" + (f" {clean_title}" if clean_title else "")
    entry = f"{heading}\n\n{body.strip()}\n"

    bounds = _find_section_bounds(markdown_text, 3)
    if bounds is None:
        base = markdown_text.rstrip()
        sep = "\n\n" if base else ""
        return f"{base}{sep}{SECTION_3_HEADER}\n\n{entry}"

    _, body_start, sec_end = bounds
    sec3_body = markdown_text[body_start:sec_end].rstrip()
    prefix = f"{sec3_body}\n\n" if sec3_body else ""
    suffix = markdown_text[sec_end:]
    trailing = f"\n{suffix.lstrip()}" if suffix.strip() else ""
    return markdown_text[:body_start] + prefix + entry + trailing


def git_commit_and_push(
    repo_dir: Path,
    rel_paths: List[str],
    message: str,
) -> str:
    """Stage `rel_paths`, commit if changed, push to `origin main`, and return short HEAD SHA."""
    def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )

    _git("add", "--", *rel_paths)
    diff_res = _git("diff", "--cached", "--quiet", check=False)
    if diff_res.returncode == 1:
        _git("commit", "-m", message)
    _git("push", "origin", "main")
    sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    return sha


def _read_body_arg(body: str, body_file: str) -> str:
    if body_file:
        return Path(body_file).read_text(encoding="utf-8", errors="replace").strip()
    return body.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync two-way handoff tasks/results via Google Docs (if configured) or GitHub HANDOFF.md."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--file", default=str(DEFAULT_HANDOFF_PATH), help="Path to local HANDOFF.md mirror.")
        p.add_argument("--doc-id", default=DEFAULT_DOC_ID, help="Google Doc ID for Antigravity_Handoff.")
        p.add_argument("--google-doc", action="store_true", help="Attempt live Google Docs API read/write.")

    p_list = sub.add_parser("list-pending", help="List PENDING `[TASK-XXX]` items from Section 2.")
    _add_common(p_list)
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_done = sub.add_parser("complete", help="Mark Section 2 task COMPLETED and append Section 3 result.")
    _add_common(p_done)
    p_done.add_argument("--task-id", required=True, help="Task ID such as TASK-001.")
    p_done.add_argument("--title", default="", help="Result heading title.")
    p_done.add_argument("--body", default="", help="Inline result markdown body.")
    p_done.add_argument("--body-file", default="", help="File containing result markdown body.")
    p_done.add_argument("--push", action="store_true", help="Commit and push HANDOFF.md to origin main.")

    p_app = sub.add_parser("append", help="Append a standalone timestamped update to Section 3.")
    _add_common(p_app)
    p_app.add_argument("--task-id", required=True, help="Tag/task ID such as TASK-001 or RUN-31.")
    p_app.add_argument("--title", default="", help="Heading title.")
    p_app.add_argument("--body", default="", help="Inline markdown body.")
    p_app.add_argument("--body-file", default="", help="File containing markdown body.")
    p_app.add_argument("--push", action="store_true", help="Commit and push HANDOFF.md to origin main.")

    p_stat = sub.add_parser("status", help="Show Google Docs bridge capability and pending count.")
    _add_common(p_stat)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handoff_path = Path(args.file)

    if getattr(args, "google_doc", False) and not google_docs_available():
        sys.stderr.write(
            f"[handoff_sync] Google Docs credentials/packages not configured for Doc {args.doc_id}; "
            f"using local mirror {handoff_path}.\n"
        )

    if not handoff_path.is_file():
        sys.stderr.write(f"error: handoff file not found: {handoff_path}\n")
        return 2

    text = handoff_path.read_text(encoding="utf-8", errors="replace")

    if args.command == "status":
        pending = parse_pending_tasks(text)
        info: Dict[str, Any] = {
            "doc_name": DEFAULT_DOC_NAME,
            "doc_id": args.doc_id,
            "google_docs_ready": google_docs_available(),
            "mirror_path": str(handoff_path),
            "pending_count": len(pending),
            "pending_ids": [t["task_id"] for t in pending],
        }
        print(json.dumps(info, indent=2))
        return 0

    if args.command == "list-pending":
        pending = parse_pending_tasks(text)
        if args.json:
            print(json.dumps(pending, indent=2))
        elif not pending:
            print("No PENDING tasks in Section 2.")
        else:
            for item in pending:
                hdr = f"[{item['task_id']}] {item['title']}".strip()
                print(f"- {hdr} (STATUS: {item['status']})")
                if item["body"]:
                    print(f"  {item['body'].splitlines()[0]}")
        return 0

    if args.command == "complete":
        ts = utc_now_str()
        body = _read_body_arg(args.body, args.body_file)
        if not body:
            sys.stderr.write("error: complete requires --body or --body-file\n")
            return 2
        updated = mark_task_status(text, args.task_id, "COMPLETED", completed_at=ts)
        updated = append_result(updated, args.task_id, args.title, body, timestamp=ts)
        handoff_path.write_text(updated, encoding="utf-8")
        print(f"[handoff_sync] Marked {args.task_id} COMPLETED and appended result -> {handoff_path}")
        if args.push:
            rel = str(handoff_path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
            sha = git_commit_and_push(REPO_ROOT, [rel], f"handoff: complete {args.task_id}")
            print(f"[handoff_sync] Pushed {rel} to origin/main ({sha})")
        return 0

    if args.command == "append":
        ts = utc_now_str()
        body = _read_body_arg(args.body, args.body_file)
        if not body:
            sys.stderr.write("error: append requires --body or --body-file\n")
            return 2
        updated = append_result(text, args.task_id, args.title, body, timestamp=ts)
        handoff_path.write_text(updated, encoding="utf-8")
        print(f"[handoff_sync] Appended {args.task_id} -> {handoff_path}")
        if args.push:
            rel = str(handoff_path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
            sha = git_commit_and_push(REPO_ROOT, [rel], f"handoff: update {args.task_id}")
            print(f"[handoff_sync] Pushed {rel} to origin/main ({sha})")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
