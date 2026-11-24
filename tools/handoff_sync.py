#!/usr/bin/env python3
"""Two-way handoff bridge for `Antigravity_Handoff` via live `gdocs` CLI and GitHub `HANDOFF.md` mirror."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HANDOFF_PATH = REPO_ROOT / "HANDOFF.md"
DEFAULT_DOC_ID = "1sYMIHG99QP_92-C9hvm4noj2huuJByfOiyDaECJnxxw"
DEFAULT_DOC_NAME = "Antigravity_Handoff"

SECTION_2_HEADER = "## Section 2: Gemini -> Antigravity"
SECTION_3_HEADER = "## Section 3: Antigravity -> Gemini"

# Matches both "## Section 2: ..." (local mirror) and "## 2. ..." (live gdocs markdown)
_SECTION_HDR_RE = re.compile(r"^##\s+(?:Section\s+)?(\d+)[.:]?\s*.*$", re.M)
_TASK_HDR_RE = re.compile(r"^###\s*\\?\[(TASK-\d+)\\?\](.*)$", re.M)
_INLINE_TASK_RE = re.compile(
    r"^\\?\[(TASK-\d+)\\?\]\s*(.*)$",
    re.M,
)
_STATUS_LINE_RE = re.compile(
    r"^(\s*(?:-\s*)?STATUS:\s*)([A-Za-z0-9_\\-]+)(.*)$",
    re.I | re.M,
)
_INLINE_STATUS_RE = re.compile(
    r"\bSTATUS:\s*([A-Za-z0-9_\\-]+)\b",
    re.I,
)
_STRUCT_LINE_RE = re.compile(r"^\s*(\d+)\s+([A-Z0-9_]+)\s+\"(.*)\"\s*$")


def utc_now_str() -> str:
    """Return current UTC timestamp formatted cleanly for handoff entries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _unescape_gdocs_md(text: str) -> str:
    return re.sub(r"\\([\[\]_#*`])", r"\1", text)


def gdocs_cli_available() -> bool:
    """Return True if the Antigravity `gdocs` plugin CLI is on PATH."""
    return shutil.which("gdocs") is not None


def google_docs_available() -> bool:
    """Return True if either `gdocs` CLI or local Google Docs Python API credentials are ready."""
    if gdocs_cli_available():
        return True
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
    """Return (header_start, body_start, section_end) offsets for section `section_num`."""
    matches = list(_SECTION_HDR_RE.finditer(markdown_text))
    for idx, m in enumerate(matches):
        if int(m.group(1)) == section_num:
            body_start = markdown_text.find("\n", m.end())
            body_start = len(markdown_text) if body_start == -1 else body_start + 1
            sec_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
            return m.start(), body_start, sec_end
    return None


def _iter_section2_tasks(section2_body: str) -> List[Tuple[str, str, int, int, str]]:
    """Return (task_id, title_suffix, start, end, block_text) for `[TASK-XXX]` blocks in Section 2."""
    hdrs = list(_TASK_HDR_RE.finditer(section2_body))
    if hdrs:
        tasks: List[Tuple[str, str, int, int, str]] = []
        for idx, m in enumerate(hdrs):
            end = hdrs[idx + 1].start() if idx + 1 < len(hdrs) else len(section2_body)
            tasks.append((m.group(1).strip(), m.group(2).strip(), m.start(), end, section2_body[m.start() : end]))
        return tasks

    inlines = list(_INLINE_TASK_RE.finditer(section2_body))
    tasks = []
    for idx, m in enumerate(inlines):
        end = inlines[idx + 1].start() if idx + 1 < len(inlines) else len(section2_body)
        tasks.append((m.group(1).strip(), "", m.start(), end, section2_body[m.start() : end]))
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
        sm = _STATUS_LINE_RE.search(block) or _INLINE_STATUS_RE.search(block)
        if not sm:
            continue
        raw_status = sm.group(2) if sm.re is _STATUS_LINE_RE else sm.group(1)
        status = _unescape_gdocs_md(raw_status).strip().upper()
        if status != "PENDING":
            continue
        clean_block = _unescape_gdocs_md(block)
        lines = clean_block.splitlines()[1:]
        body_lines = [ln for ln in lines if not _STATUS_LINE_RE.match(ln)]
        body = "\n".join(body_lines).strip()
        if not body and "ACTION:" in clean_block:
            body = clean_block.split("ACTION:", 1)[1].strip()
        pending.append(
            {
                "task_id": task_id,
                "status": status,
                "title": _unescape_gdocs_md(title_suffix).lstrip("-: ").strip(),
                "body": body,
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
            if norm_status in ("COMPLETED", "DONE") and completed_at:
                tail = re.sub(r"\s*\|\s*COMPLETED_AT:.*$", "", tail).rstrip()
                tail = f"{tail} | COMPLETED_AT: {completed_at}"
            updated_block = block[: sm.start()] + f"{sm.group(1)}{norm_status}{tail}" + block[sm.end() :]
        else:
            ism = _INLINE_STATUS_RE.search(block)
            if ism:
                updated_block = block[: ism.start(1)] + norm_status + block[ism.end(1) :]
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
    entry = f"{heading}\n\n{body.strip()}\n" if body.strip() else f"{heading}\n"

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


# ---------------------------------------------------------------------------
# Live Google Docs CLI (`gdocs`) Helpers
# ---------------------------------------------------------------------------


def _run_gdocs(args: List[str]) -> subprocess.CompletedProcess[str]:
    gdocs_exe = shutil.which("gdocs")
    if not gdocs_exe:
        raise RuntimeError("`gdocs` CLI not found on PATH. Ensure the Google Docs plugin is enabled and initialized.")
    cmd = ["cmd", "/c", "gdocs", *args] if gdocs_exe.lower().endswith((".cmd", ".bat")) else [gdocs_exe, *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def gdocs_read_markdown(doc_id: str) -> str:
    """Read live Google Doc contents as Markdown using `gdocs readonly read`."""
    return _run_gdocs(["readonly", "read", doc_id]).stdout


def gdocs_read_url(doc_id: str) -> str:
    """Fetch canonical edit URL for a Google Doc."""
    return _run_gdocs(["readonly", "get-url", doc_id]).stdout.strip()


def parse_gdocs_structure(struct_output: str) -> List[Tuple[int, str, str]]:
    """Parse `gdocs readonly structure` output into [(elem_idx, elem_type, preview_text)]."""
    items: List[Tuple[int, str, str]] = []
    for line in struct_output.splitlines():
        m = _STRUCT_LINE_RE.match(line)
        if m:
            items.append((int(m.group(1)), m.group(2), m.group(3)))
    return items


def gdocs_read_structure(doc_id: str) -> List[Tuple[int, str, str]]:
    """Return parsed paragraph/element structure for `doc_id`."""
    return parse_gdocs_structure(_run_gdocs(["readonly", "structure", doc_id]).stdout)


def gdocs_find_task_elem(structure: List[Tuple[int, str, str]], task_id: str) -> int:
    """Locate the element index in Section 2 containing `[<task_id>]`."""
    in_sec2 = False
    tag = f"[{task_id}]"
    for elem_idx, elem_type, text in structure:
        if elem_type == "HEADING_2" and text.startswith("2."):
            in_sec2 = True
            continue
        if in_sec2 and elem_type == "HEADING_2" and not text.startswith("2."):
            break
        if in_sec2 and tag in text:
            return elem_idx
    # Fallback search anywhere if headings differ
    for elem_idx, _, text in structure:
        if tag in text:
            return elem_idx
    raise KeyError(f"Could not find {tag!r} in Google Doc structure.")


def gdocs_find_section3_insert_anchor(structure: List[Tuple[int, str, str]]) -> int:
    """Return the last non-spacer element index inside Section 3 before Section 4."""
    in_sec3 = False
    last_sec3 = -1
    last_nonblank_sec3 = -1
    for elem_idx, elem_type, text in structure:
        if elem_type == "HEADING_2" and text.startswith("3."):
            in_sec3 = True
            last_sec3 = elem_idx
            continue
        if in_sec3 and elem_type == "HEADING_2":
            break
        if in_sec3:
            last_sec3 = elem_idx
            stripped = text.replace("\\u00a0", "").replace("\u00a0", "").strip()
            if stripped:
                last_nonblank_sec3 = elem_idx
    if last_sec3 < 0:
        raise ValueError("Could not find Section 3 heading (`3. ...`) in Google Doc structure.")
    return last_nonblank_sec3 if last_nonblank_sec3 >= 0 else last_sec3


def gdocs_set_task_status(doc_id: str, task_id: str, new_status: str) -> None:
    """Update `STATUS: ...` for `task_id` inside live Google Doc Section 2."""
    norm = new_status.strip().upper()
    struct = gdocs_read_structure(doc_id)
    elem_idx = gdocs_find_task_elem(struct, task_id)
    expr = f"{elem_idx}s/STATUS:\\s*(PENDING|IN_PROGRESS|DONE|COMPLETED)/STATUS: {norm}/"
    _run_gdocs(["mutate", "sed", doc_id, "-e", expr, "--regex", "--first"])


def gdocs_append_section3(
    doc_id: str,
    task_id: str,
    title: str,
    body: str,
    timestamp: Optional[str] = None,
) -> None:
    """Append timestamped `[<task_id>]` lines cleanly inside Section 3 of the live Google Doc."""
    ts = timestamp or utc_now_str()
    clean_title = title.strip()
    header_line = f"[{ts}] [{task_id.strip()}]" + (f" {clean_title}" if clean_title else "")
    body_lines = [ln.rstrip() for ln in body.strip().splitlines() if ln.strip()]
    all_lines = [header_line, *body_lines]

    struct = gdocs_read_structure(doc_id)
    anchor = gdocs_find_section3_insert_anchor(struct)

    # Reverse-order `<anchor>a\` insertions keep paragraphs in exact forward order after `anchor`.
    sed_exprs = [f"{anchor}a\\{ln}" for ln in reversed(all_lines)]
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".sed",
        delete=False,
    ) as tmp:
        tmp.write("\n".join(sed_exprs) + "\n")
        sed_path = Path(tmp.name)
    try:
        _run_gdocs(["mutate", "sed", doc_id, "-f", str(sed_path)])
    finally:
        try:
            sed_path.unlink()
        except OSError:
            pass


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
    return _git("rev-parse", "--short", "HEAD").stdout.strip()


def _read_body_arg(body: str, body_file: str) -> str:
    if body_file:
        return Path(body_file).read_text(encoding="utf-8", errors="replace").strip()
    return body.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync two-way handoff tasks/results via live `gdocs` CLI and GitHub `HANDOFF.md`."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--file", default=str(DEFAULT_HANDOFF_PATH), help="Path to local HANDOFF.md mirror.")
        p.add_argument("--doc-id", default=DEFAULT_DOC_ID, help="Google Doc ID for Antigravity_Handoff.")
        p.add_argument("--google-doc", action="store_true", help="Read/write live Google Doc via `gdocs` CLI.")

    p_list = sub.add_parser("list-pending", help="List PENDING `[TASK-XXX]` items from Section 2.")
    _add_common(p_list)
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_set = sub.add_parser("set-status", help="Update a Section 2 task status (PENDING, IN_PROGRESS, DONE).")
    _add_common(p_set)
    p_set.add_argument("--task-id", required=True, help="Task ID such as TASK-001.")
    p_set.add_argument("--status", required=True, help="Target status: PENDING, IN_PROGRESS, DONE, COMPLETED.")
    p_set.add_argument("--push", action="store_true", help="Commit and push HANDOFF.md mirror to origin main.")

    p_done = sub.add_parser("complete", help="Mark Section 2 task DONE/COMPLETED and append Section 3 result.")
    _add_common(p_done)
    p_done.add_argument("--task-id", required=True, help="Task ID such as TASK-001.")
    p_done.add_argument("--title", default="", help="Result heading title.")
    p_done.add_argument("--body", default="", help="Inline result markdown body.")
    p_done.add_argument("--body-file", default="", help="File containing result markdown body.")
    p_done.add_argument("--push", action="store_true", help="Commit and push HANDOFF.md mirror to origin main.")

    p_app = sub.add_parser("append", help="Append a standalone timestamped update to Section 3.")
    _add_common(p_app)
    p_app.add_argument("--task-id", required=True, help="Tag/task ID such as TASK-001 or RUN-31.")
    p_app.add_argument("--title", default="", help="Heading title.")
    p_app.add_argument("--body", default="", help="Inline markdown body.")
    p_app.add_argument("--body-file", default="", help="File containing markdown body.")
    p_app.add_argument("--push", action="store_true", help="Commit and push HANDOFF.md mirror to origin main.")

    p_stat = sub.add_parser("status", help="Show Google Docs bridge capability and pending count.")
    _add_common(p_stat)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handoff_path = Path(args.file)
    use_gdocs = bool(args.google_doc and gdocs_cli_available())

    if args.google_doc and not use_gdocs:
        sys.stderr.write(
            f"[handoff_sync] `gdocs` CLI not available for Doc {args.doc_id}; using local mirror {handoff_path}.\n"
        )

    if not handoff_path.is_file() and not use_gdocs:
        sys.stderr.write(f"error: handoff file not found: {handoff_path}\n")
        return 2

    if args.command == "status":
        source_text = gdocs_read_markdown(args.doc_id) if use_gdocs else handoff_path.read_text(encoding="utf-8", errors="replace")
        pending = parse_pending_tasks(source_text)
        info: Dict[str, Any] = {
            "doc_name": DEFAULT_DOC_NAME,
            "doc_id": args.doc_id,
            "gdocs_cli_ready": gdocs_cli_available(),
            "google_docs_ready": google_docs_available(),
            "source": "google_doc" if use_gdocs else "local_mirror",
            "mirror_path": str(handoff_path),
            "pending_count": len(pending),
            "pending_ids": [t["task_id"] for t in pending],
        }
        print(json.dumps(info, indent=2))
        return 0

    if args.command == "list-pending":
        source_text = gdocs_read_markdown(args.doc_id) if use_gdocs else handoff_path.read_text(encoding="utf-8", errors="replace")
        pending = parse_pending_tasks(source_text)
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

    if args.command == "set-status":
        ts = utc_now_str()
        if use_gdocs:
            gdocs_set_task_status(args.doc_id, args.task_id, args.status)
            print(f"[handoff_sync] Updated live Google Doc {args.task_id} -> STATUS: {args.status.upper()}")
        if handoff_path.is_file():
            text = handoff_path.read_text(encoding="utf-8", errors="replace")
            updated = mark_task_status(text, args.task_id, args.status, completed_at=ts if args.status.upper() in ("DONE", "COMPLETED") else None)
            handoff_path.write_text(updated, encoding="utf-8")
            print(f"[handoff_sync] Updated mirror {args.task_id} -> {args.status.upper()} ({handoff_path})")
            if args.push:
                rel = str(handoff_path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
                sha = git_commit_and_push(REPO_ROOT, [rel], f"handoff: {args.task_id} -> {args.status.upper()}")
                print(f"[handoff_sync] Pushed {rel} to origin/main ({sha})")
        return 0

    if args.command == "complete":
        ts = utc_now_str()
        body = _read_body_arg(args.body, args.body_file)
        if not body:
            sys.stderr.write("error: complete requires --body or --body-file\n")
            return 2
        if use_gdocs:
            gdocs_set_task_status(args.doc_id, args.task_id, "DONE")
            gdocs_append_section3(args.doc_id, args.task_id, args.title, body, timestamp=ts)
            url = gdocs_read_url(args.doc_id)
            print(f"[handoff_sync] Marked {args.task_id} DONE & appended to live Google Doc: {url}")
        if handoff_path.is_file():
            text = handoff_path.read_text(encoding="utf-8", errors="replace")
            updated = mark_task_status(text, args.task_id, "COMPLETED", completed_at=ts)
            updated = append_result(updated, args.task_id, args.title, body, timestamp=ts)
            handoff_path.write_text(updated, encoding="utf-8")
            print(f"[handoff_sync] Updated mirror -> {handoff_path}")
            if args.push:
                rel = str(handoff_path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
                sha = git_commit_and_push(REPO_ROOT, [rel], f"handoff: complete {args.task_id}")
                print(f"[handoff_sync] Pushed {rel} to origin/main ({sha})")
        return 0

    if args.command == "append":
        ts = utc_now_str()
        body = _read_body_arg(args.body, args.body_file)
        if not body and not args.title.strip():
            sys.stderr.write("error: append requires --body, --body-file, or --title\n")
            return 2
        if use_gdocs:
            gdocs_append_section3(args.doc_id, args.task_id, args.title, body, timestamp=ts)
            url = gdocs_read_url(args.doc_id)
            print(f"[handoff_sync] Appended {args.task_id} to live Google Doc: {url}")
        if handoff_path.is_file():
            text = handoff_path.read_text(encoding="utf-8", errors="replace")
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
