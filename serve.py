"""Official PLE Serve Protocol v1 agent server (Plan-Work-Verify + MCP + CircuitBreaker).

Binds `0.0.0.0:$PORT` via `taskbridge` (`max_size=16 MiB`, `ping_interval=None`)
and executes tasks inside the isolated task container using OpenAI-compatible
model endpoints injected by the PLE evaluator (`MODEL_NAME`, `OPENAI_BASE_URL`,
`OPENAI_API_KEY`, `MODEL_BASE_URL`, `MODEL_API_KEY`, `GOOGLE_GEMINI_BASE_URL`,
`GEMINI_API_KEY`).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import posixpath
import re
import signal
import sys
import urllib.parse
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import openai
from websockets.asyncio.server import serve as _ws_serve

import taskbridge
from harness import (
    CircuitBreaker,
    DEFAULT_MODEL,
    _safe_int,
    evaluate_pre_pass_gate,
    extract_json_payload,
    is_pass_verdict,
    load_submission_config,
    normalize_model_name,
)

logger = logging.getLogger(__name__)

MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
WRAP_UP_SEC = 240
WRAP_UP = {
    "role": "user",
    "content": "Time is up for this step. Stop and reply with your answer.",
}
VERIFIER_WRAP_UP = {
    "role": "user",
    "content": (
        "Time or turn budget is up for this verification step. Stop tool calls and "
        "reply now: ONLY output PASS if you have ALREADY inspected every required "
        "deliverable file on disk and verified every calculation/constraint; "
        "otherwise list every unchecked item and every detected gap as a numbered list."
    ),
}
RESULT_CHARS = 20_000
EXEC_INLINE_CAP_CHARS = 6_000
EXEC_SPILL_HEAD_CHARS = 3_400
EXEC_SPILL_TAIL_CHARS = 2_000
PRUNE_TOOL_AGE_TURNS = 8
PRUNE_TOOL_MIN_CHARS = 4_000
PRUNE_REF_WINDOW_TURNS = 4
PRUNE_STUB_CHARS = 300
PRUNE_CHECKPOINT_EVERY = 12
MAX_IMAGE_BYTES = 5 << 20
MAX_ACTIVE_IMAGES = 10
IMAGE_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)
RETRY_WAITS = (5, 15, 30, 60, 120)
CONTEXT_FULL = re.compile(
    r"context.?(length|window|size|limit)|maximum context|too many tokens"
    r"|prompt is too long|maximum prompt length"
    r"|(input|prompt|request|messages?)\b[^.]{0,40}(too long|exceed)"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
UNSUPPORTED = re.compile(
    r"unsupported|unrecogni[sz]ed|unknown|not supported|does not support"
    r"|not (allowed|permitted)|extra (inputs|fields)|unexpected",
    re.I,
)
CUT_KEEP = 1000
MAX_NUDGES = 3
PLAN_SHARE = 0.10
EXTRACT_SHARE = 0.04
CHECK_SHARE = 0.25
MIN_STEP_SEC = 120
DIRECT_MCP_CHARS = 40_000
MAX_TOOLS = 128
EXTRACTOR_WRAP_UP = {
    "role": "user",
    "content": (
        "Emit CONSTRAINTS.json now as a single JSON object without calling another tool."
    ),
}

PLANNER = (
    "You plan an engineering task that another engineer will carry out in a "
    "Linux container with no internet access. Inspect the task's files with "
    "your tools (bash starts in {workdir}), but do not create or change any "
    "file. For PNG/JPEG/GIF/WEBP engineering drawings, schedules, or diagrams, "
    "call view_image instead of writing pixel/ASCII OCR scripts. Keep your "
    "inspection concise so you stay well within your turn budget, then reply "
    "with the plan: each deliverable with its exact path and format, every hard "
    "constraint / threshold / register or tag ID format, the data and method "
    "to use, and concrete self-verification checks (for spreadsheets/CSVs: schema, "
    "header/row alignment, non-null IDs, totals; for IFC/BIM models: schema "
    "validation, ifcopenshell.geom.tree BVH clash checks across all elements with "
    "5-10 mm non-touching joint gaps, coordinate clearances, and boundary checks)."
)

EXTRACTOR = (
    "You extract strict engineering constraints and verification requirements from a "
    "task and its plan. Do not create or edit files or spend more than 2 tool turns "
    "(working dir {workdir}); if the prompt and plan already supply the constraints, "
    "reply immediately with a single valid JSON object representing CONSTRAINTS.json "
    "containing keys: deliverables (exact file paths and external MCP targets), "
    "required_register_ids (every requirement, finding, hazard, or scenario ID such as "
    "R01..R99, R-xxxxxx-xx, R-ILLUM, H01.., S01.. referenced in the task, plan, or spec), "
    "forbidden_citations (any standards/codes explicitly excluded by the task such as UFC), "
    "allowed_sections (explicitly listed allowed spec/manual section numbers, if specified), "
    "disallowed_sections (explicitly excluded section numbers, if any), "
    "scope_exclusions (explicit scope exclusions that must be documented), "
    "hard_constraints ([h=1] rules, prohibitions, record-date vs contingent-action "
    "distinctions, empty-set triggers, governing-scenario / load-case label rules), "
    "hard_numeric_caps (exact <= cap, >= target, and controlling boundary limits), "
    "numeric_thresholds, iso_dates, entities, and verification_checks. Output JSON only."
)

WORKER = (
    "You are an expert engineer working on a task in a Linux container with "
    "no internet access. Your tools act inside it, and bash starts in "
    "{workdir}. A plan and Extracted Constraints (CONSTRAINTS.json) follow; "
    "satisfy EVERY constraint in CONSTRAINTS.json strictly, and correct the "
    "plan where the files show it is wrong. For image files (drawings, schedules, "
    "plots), use view_image rather than pixel/ASCII OCR scripts (if a sheet is "
    "dense, crop/zoom regions to a PNG under /tmp and inspect with view_image). "
    "Compute with code, not by hand. Watch the [Turn X/Y] pacing indicator "
    "on tool outputs: never spend most of your budget exploring while leaving "
    "placeholder stubs untouched—write a working version of every required "
    "deliverable file on disk before half your turns are spent, then iterate "
    "and verify. Separate record-date posture from contingent future actions "
    "(if an output list asks what can be released/approved as of the record date "
    "and any prerequisite is pending, explicitly state that list is empty / zero "
    "and never list pending items under releasable headers). When writing tracking "
    "comments, journals, or status notes to external stores (OpenProject, Wiki, etc.), "
    "format per-entity lines unambiguously as `Entity: Final_Status` using only the "
    "authoritative final state—never echo superseded/historical conflicting status "
    "words (e.g., do not write 'pre-marked carried' on a 'NOT CARRIED' line). "
    "Before replying: (1) programmatically verify every numerical "
    "hard constraint, threshold, summation, and cross-table ID / register / tag "
    "consistency (register EVERY requirement ID from the task/project docs including "
    "alphanumeric IDs like R-ILLUM because unregistered IDs forfeit engineering credit, "
    "zero missing/shifted columns, zero NaN/N/A/TBD or unresolved '[_____]'/'*as indicated' "
    "artifacts, and on optimization/sizing tasks with hard resource caps run a discrete search "
    "preserving positive margins with >= 0.5% buffer where feasible); (2) if creating or modifying .ifc BIM deliverables, run "
    "ifcopenshell.validate plus ifcopenshell.geom.tree BVH clash checks across "
    "ALL elements (including same-service MEP segments—never exempt "
    "IfcRelConnects* links or 0 mm touching butt-joints). Because BVH flags "
    "0 mm touching faces as hard clashes, trim L-corners, T-junctions, and "
    "inline segment joints to leave clean 5-10 mm non-touching air gaps (well "
    "within connectivity joint tolerance such as 250 mm) so BVH reports zero "
    "clashes while EVERY service network forms 1 connected component (<= 250 mm), "
    "do not tag detached termination/drop proxies (> 250 mm away) with parent "
    "route service tags (`CT-*`, `BW-*`, `CHW-*`), and verify all code "
    "clearances from tessellated 3D geometry (tray-to-busway >= 300 mm, "
    "tray-stack/access headroom >= 300 mm above/below trays per TIA-569-B, "
    "overhead clear height >= 2600 mm AFF unless explicitly permitted, "
    "sprinkler clearance >= 457 mm, PDU clear zones, and site building/road "
    "non-intersection). When routing MEP networks or resolving clash, separation, "
    "or headroom violations, keep runs compact and direct—prefer minimal local "
    "lateral offsets, clean parallel lane allocation, or valid elevation/zone "
    "separation over perimeter detours or redundant backtracks that bloat total "
    "routed length and degrade length-efficiency objectives. "
    "After any edit or fix, immediately regenerate and re-run "
    "verification checks on the actual deliverable file before replying. When "
    "done, reply without calling a tool: summarize each deliverable, its exact "
    "path, and the verified results."
)

VERIFIER = (
    "You check an engineer's work on a task in a Linux container. You get the "
    "task, the plan, Extracted Constraints (CONSTRAINTS.json), the engineer's "
    "summary of what was delivered, and an automated Programmatic Pre-Check summary. "
    "Open and check every required deliverable with your tools (bash starts in "
    "{workdir}; use view_image when inspecting images or rendered plots) against "
    "the task and CONSTRAINTS.json: verify each deliverable file exists on disk, "
    "is NOT an unmodified template/stub, reflects the latest script edits, "
    "matches the exact path, headers, and format, respects every record-date / "
    "empty-set / scope rule verbatim, has no shifted CSV columns, formula errors, "
    "duplicate rows, or missing/unpopulated register/device/required IDs (verify "
    "every required_register_ids entry is covered in any constraint_register file), "
    "and satisfies every hard constraint and tolerance recomputed directly from the "
    "file. Using Python directly against the final deliverable tables on disk "
    "(never trusting narrative summaries), independently recompute: (a) all "
    "governing scenario / load-case labels to confirm the reported governing label "
    "matches the true controlling extreme across all scenarios in the table, and "
    "(b) every hard numeric cap (<= cap, >= target), closely scrutinizing any "
    "near-boundary values within ~5% of a limit from raw table numbers. "
    "Whenever external MCP stores were modified (e.g., OpenProject work packages/"
    "comments/journals, Nextcloud, Wiki), read back the actual persisted MCP "
    "records and comments/journals and verify single-valued entity consistency "
    "against delivered files verbatim—reject PASS on any ambiguity or contradiction. "
    "For .ifc deliverables, run Python checks using ifcopenshell.geom.tree BVH "
    "across ALL elements without exempting IfcRelConnects* or 0 mm butt-joints "
    "(verify 5-10 mm non-touching gaps at L-corners/T-junctions/joints so BVH clash "
    "count is 0 while each service forms 1 connected component <= 250 mm) plus 3D "
    "clearances (tray-to-busway >= 300 mm, tray-stack headroom >= 300 mm, overhead "
    "clear height >= 2600 mm AFF, sprinkler >= 457 mm, PDU clear zones, road vs. "
    "building non-intersection) and inspect every Programmatic Pre-Check warning "
    "rather than trusting the summary. Do not create or change any file. If every "
    "requirement, external readback, and pre-check passes with zero errors, reply "
    "with the single word PASS and nothing else. Otherwise reply with a numbered "
    "list of every gap, each specific enough to fix."
)


def tool(name: str, description: str, required: List[str], **properties: Any) -> Dict[str, Any]:
    """Build an OpenAI function-tool definition dictionary."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


TOOLS: List[Dict[str, Any]] = [
    tool(
        "bash",
        "Run a bash command (default 120 s limit) and return its exit code and output.",
        ["command"],
        command={"type": "string"},
        timeout_sec={"type": "integer", "description": "Timeout in seconds."},
    ),
    tool(
        "read_file",
        "Read a text file from the task container.",
        ["path"],
        path={"type": "string"},
    ),
    tool(
        "view_image",
        (
            "Inspect an image file (PNG, JPEG, GIF, or WEBP, up to 5 MiB) visually. "
            "Use this for engineering drawings, schedules, diagrams, and plots "
            "instead of pixel-by-pixel text OCR."
        ),
        ["path"],
        path={"type": "string", "description": "Path to the image file in the task container."},
    ),
    tool(
        "write_file",
        "Create or overwrite a file in the task container; parent folders are created.",
        ["path", "content"],
        path={"type": "string"},
        content={"type": "string"},
        append={"type": "boolean", "description": "Append to existing file instead."},
    ),
    tool(
        "edit_file",
        "Replace exact text in a file; old_text must match uniquely unless replace_all is true.",
        ["path", "old_text", "new_text"],
        path={"type": "string"},
        old_text={"type": "string"},
        new_text={"type": "string"},
        replace_all={"type": "boolean"},
    ),
]
READ_ONLY: List[Dict[str, Any]] = TOOLS[:3]
MCP_CATALOG_TOOLS: List[Dict[str, Any]] = [
    tool(
        "mcp_find",
        "Search available task MCP tools by keyword or name.",
        ["query"],
        query={"type": "string"},
    ),
    tool(
        "mcp_call",
        "Invoke a task MCP tool by registered name with a JSON arguments object.",
        ["tool", "arguments"],
        tool={"type": "string"},
        arguments={"type": "object"},
    ),
]


def _normalize_gemini_openai_base(url: str) -> str:
    """Convert a native Gemini base URL (or proxy origin) to an OpenAI v1 base URL."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if urllib.parse.urlparse(cleaned).hostname == "generativelanguage.googleapis.com":
        if cleaned.endswith("/openai") or cleaned.endswith("/v1beta/openai"):
            return cleaned
        if cleaned.endswith("/v1beta"):
            return f"{cleaned}/openai"
        return f"{cleaned}/v1beta/openai"
    if cleaned.endswith("/v1") or cleaned.endswith("/openai"):
        return cleaned
    return f"{cleaned}/v1"


def resolve_runtime_config(config_path: str = "submission.yaml") -> Dict[str, Any]:
    """Resolve model endpoint, credentials, and harness settings lazily from env + yaml."""
    cfg = load_submission_config(config_path)
    env_cfg = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}

    env_model = os.environ.get("MODEL_NAME", "").strip()
    if env_model:
        model = (
            normalize_model_name(env_model)
            if env_model.startswith("google-cloud/")
            else env_model
        )
    else:
        raw_model = cfg.get("model_api") if "model_api" in cfg else cfg.get("model", DEFAULT_MODEL)
        model = normalize_model_name(raw_model)

    openai_base = os.environ.get("OPENAI_BASE_URL", "").strip()
    model_base = os.environ.get("MODEL_BASE_URL", "").strip()
    gemini_base = os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip()

    if openai_base:
        base_url = openai_base.rstrip("/")
    elif model_base:
        base_url = model_base.rstrip("/")
    elif gemini_base:
        base_url = _normalize_gemini_openai_base(gemini_base)
    else:
        base_url = ""

    api_key = (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("MODEL_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or ""
    )

    effort_raw = os.environ.get("REASONING_EFFORT", "").strip() or str(
        env_cfg.get("REASONING_EFFORT") or ""
    ).strip()
    effort = effort_raw or (
        "max" if any(m in model.lower() for m in MAX_EFFORT_MODELS) else "high"
    )

    max_output_tokens = _safe_int(
        os.environ.get("MAX_OUTPUT_TOKENS") or env_cfg.get("MAX_OUTPUT_TOKENS"),
        default=32000,
        minimum=256,
    )
    max_fix_rounds = _safe_int(
        os.environ.get("MAX_FIX_ROUNDS") or env_cfg.get("MAX_FIX_ROUNDS"),
        default=2,
        minimum=0,
    )
    max_retries = _safe_int(
        os.environ.get("MAX_RETRIES") or env_cfg.get("MAX_RETRIES"),
        default=3,
        minimum=1,
    )
    max_failures = _safe_int(
        os.environ.get("MAX_FAILURES") or env_cfg.get("MAX_FAILURES"),
        default=3,
        minimum=1,
    )
    max_plan_turns = _safe_int(
        os.environ.get("MAX_PLAN_TURNS") or env_cfg.get("MAX_PLAN_TURNS"),
        default=18,
        minimum=2,
    )
    max_extract_turns = min(
        3,
        _safe_int(
            os.environ.get("MAX_EXTRACT_TURNS") or env_cfg.get("MAX_EXTRACT_TURNS"),
            default=3,
            minimum=1,
        ),
    )
    max_work_turns = _safe_int(
        os.environ.get("MAX_WORK_TURNS") or env_cfg.get("MAX_WORK_TURNS"),
        default=75,
        minimum=2,
    )
    max_verify_turns = _safe_int(
        os.environ.get("MAX_VERIFY_TURNS") or env_cfg.get("MAX_VERIFY_TURNS"),
        default=25,
        minimum=2,
    )
    max_fix_turns = _safe_int(
        os.environ.get("MAX_FIX_TURNS") or env_cfg.get("MAX_FIX_TURNS"),
        default=30,
        minimum=2,
    )
    model_call_timeout_sec = _safe_int(
        os.environ.get("MODEL_CALL_TIMEOUT_SEC")
        or env_cfg.get("MODEL_CALL_TIMEOUT_SEC"),
        default=600,
        minimum=30,
    )

    raw_fallbacks: Any = os.environ.get("FALLBACK_MODELS", "").strip()
    if not raw_fallbacks:
        raw_fallbacks = env_cfg.get("FALLBACK_MODELS")
    if not raw_fallbacks:
        harness_cfg = cfg.get("harness") if isinstance(cfg.get("harness"), dict) else {}
        raw_fallbacks = cfg.get("fallback_models") or harness_cfg.get("fallback_models")
    fb_candidates: List[str] = []
    if isinstance(raw_fallbacks, str):
        fb_candidates = [item.strip() for item in raw_fallbacks.split(",") if item.strip()]
    elif isinstance(raw_fallbacks, (list, tuple)):
        fb_candidates = [str(item).strip() for item in raw_fallbacks if item is not None and str(item).strip()]
    fallback_models: List[str] = []
    for cand in fb_candidates:
        norm_fb = normalize_model_name(cand) if cand.startswith("google-cloud/") else cand
        if norm_fb and norm_fb != model and norm_fb not in fallback_models:
            fallback_models.append(norm_fb)

    return {
        "model": model,
        "fallback_models": fallback_models,
        "base_url": base_url,
        "api_key": api_key,
        "effort": effort,
        "max_output_tokens": max_output_tokens,
        "max_fix_rounds": max_fix_rounds,
        "max_retries": max_retries,
        "max_failures": max_failures,
        "max_plan_turns": max_plan_turns,
        "max_extract_turns": max_extract_turns,
        "max_work_turns": max_work_turns,
        "max_verify_turns": max_verify_turns,
        "max_fix_turns": max_fix_turns,
        "model_call_timeout_sec": model_call_timeout_sec,
        "cache_mark": "claude" in model.lower(),
    }


def build_openai_client(runtime_cfg: Dict[str, Any]) -> openai.AsyncOpenAI:
    """Create a lazy AsyncOpenAI client from resolved runtime configuration."""
    return openai.AsyncOpenAI(
        base_url=runtime_cfg.get("base_url") or None,
        api_key=runtime_cfg.get("api_key") or "none",
        max_retries=5,
        timeout=runtime_cfg.get("model_call_timeout_sec", 600),
    )


def clip(text: str, limit: int = RESULT_CHARS) -> str:
    """Truncate `text` in the middle if it exceeds `limit` characters."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n{text[-half:]}"
    )


def marked(messages: List[Dict[str, Any]], cache_mark: bool = False) -> List[Dict[str, Any]]:
    """Mark system and latest messages with `cache_control` when using Claude."""
    if not cache_mark or not messages:
        return messages
    out = list(messages)
    for i in {0, len(out) - 1}:
        m, content = out[i], out[i].get("content")
        if isinstance(content, str) and content:
            out[i] = {
                **m,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        elif isinstance(content, list) and content:
            parts = list(content)
            if isinstance(parts[-1], dict):
                parts[-1] = {**parts[-1], "cache_control": {"type": "ephemeral"}}
            out[i] = {**m, "content": parts}
        else:
            out[i] = {**m, "cache_control": {"type": "ephemeral"}}
    return out


def _detect_image_mime(data: bytes) -> Optional[str]:
    """Detect supported raster image MIME type from file header bytes."""
    for magic, mime in IMAGE_MAGIC:
        if data.startswith(magic):
            if magic == b"RIFF":
                return mime if len(data) >= 12 and data[8:12] == b"WEBP" else None
            return mime
    return None


def _prune_old_images(
    messages: List[Dict[str, Any]],
    max_images: int = MAX_ACTIVE_IMAGES,
) -> int:
    """Keep only the newest `max_images` `image_url` blocks in user messages."""
    kept = 0
    pruned = 0
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for idx in range(len(content) - 1, -1, -1):
            part = content[idx]
            if isinstance(part, dict) and part.get("type") == "image_url":
                if kept < max_images:
                    kept += 1
                else:
                    content[idx] = {
                        "type": "text",
                        "text": "[Earlier image omitted to preserve context; rely on notes/measurements already extracted instead of reloading]",
                    }
                    pruned += 1
    return pruned


_PRUNE_BOILERPLATE_TOKENS = frozenset(
    {
        "exit_code", "traceback", "python3", "filesystem", "warning", "directory",
        "section", "standard", "output", "submission", "submission.zip", "command",
        "timeout_sec", "truncated", "errors", "warnings", "indicated",
    }
)
_PRUNE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_.-]{7,}\b")
_PACING_TAIL_RE = re.compile(r"\n+\[(?:Turn \d+/\d+|Pacing:|URGENT:|\d+ minutes left)[^\n]*$")


def prune_old_tool_outputs(
    messages: List[Dict[str, Any]],
    *,
    max_age_turns: int = PRUNE_TOOL_AGE_TURNS,
    min_chars: int = PRUNE_TOOL_MIN_CHARS,
    ref_window_turns: int = PRUNE_REF_WINDOW_TURNS,
    stub_chars: int = PRUNE_STUB_CHARS,
) -> Tuple[int, int]:
    """Compact old (> max_age_turns) large (> min_chars) unreferenced tool outputs into short stubs."""
    turn_idx = -1
    msg_turns: List[int] = []
    call_id_to_tool: Dict[str, str] = {}

    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            turn_idx += 1
            for tc in msg.get("tool_calls") or ():
                if isinstance(tc, dict):
                    cid = str(tc.get("id") or "")
                    fn = str(((tc.get("function") or {}) if isinstance(tc.get("function"), dict) else {}).get("name") or "")
                    if cid:
                        call_id_to_tool[cid] = "exec" if fn in ("bash", "exec") else (fn or "tool")
        msg_turns.append(max(0, turn_idx))

    latest_turn = max(msg_turns, default=0)
    if latest_turn <= max_age_turns:
        return 0, 0

    recent_cutoff = latest_turn - max(1, ref_window_turns)
    recent_parts: List[str] = []
    for m_turn, msg in zip(msg_turns, messages):
        if m_turn > recent_cutoff:
            c = msg.get("content")
            if isinstance(c, str):
                recent_parts.append(c)
            elif isinstance(c, list):
                recent_parts.extend(str(p.get("text") or "") for p in c if isinstance(p, dict))
            for tc in msg.get("tool_calls") or ():
                if isinstance(tc, dict):
                    fn_d = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    recent_parts.append(str(fn_d.get("arguments") or ""))

    recent_text = "\n".join(recent_parts)
    recent_text_low = recent_text.lower()
    recent_tokens = {
        tok for tok in _PRUNE_TOKEN_RE.findall(recent_text_low) if tok not in _PRUNE_BOILERPLATE_TOKENS
    }

    pruned_count = 0
    saved_chars = 0
    for m_turn, msg in zip(msg_turns, messages):
        if msg.get("role") != "tool":
            continue
        age = latest_turn - m_turn
        if age <= max_age_turns:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= min_chars or content.startswith("[pruned "):
            continue

        call_id = str(msg.get("tool_call_id") or "")
        if len(call_id) >= 4 and call_id in recent_text:
            continue

        body_clean = _PACING_TAIL_RE.sub("", content)
        toks = sorted(
            {
                tok
                for tok in _PRUNE_TOKEN_RE.findall(body_clean.lower()[:20000])
                if tok not in _PRUNE_BOILERPLATE_TOKENS
            },
            key=lambda t: (-len(t), t),
        )[:24]
        if toks and len(set(toks) & recent_tokens) >= 2:
            continue

        tool_label = call_id_to_tool.get(call_id, "exec")
        orig_len = len(content)
        spill_match = re.search(r"/tmp/ple_exec_out_[0-9a-f]+\.txt", content)
        spill_note = f" | spill={spill_match.group(0)}" if spill_match else ""
        header = f"[pruned {orig_len:,}-char {tool_label} output{spill_note}; re-run if needed]"

        budget = max(40, stub_chars - len(header) - 8)
        head_len = budget // 2
        tail_len = max(20, budget - head_len)
        flat = " ".join(body_clean.split())
        head = flat[:head_len]
        tail = flat[-tail_len:] if len(flat) > head_len + tail_len else ""
        stub = f"{header}\n{head}\n...\n{tail}" if tail else f"{header}\n{head}"

        msg["content"] = stub
        pruned_count += 1
        saved_chars += orig_len - len(stub)

    return pruned_count, saved_chars


async def _format_or_spill_exec_output(
    session: taskbridge.Session,
    raw_output: str,
    *,
    inline_cap: int = EXEC_INLINE_CAP_CHARS,
) -> str:
    """Keep concise exec output inline; spill oversized output to /tmp to preserve prompt cache."""
    if len(raw_output) <= inline_cap:
        return raw_output
    digest = hashlib.sha256(raw_output.encode("utf-8", errors="replace")).hexdigest()[:10]
    spill_path = f"/tmp/ple_exec_out_{digest}.txt"
    spilled = False
    if hasattr(session, "write_file"):
        try:
            await session.write_file(spill_path, raw_output)
            spilled = True
        except Exception:
            spilled = False
    marker = (
        f"\n... [spilled {len(raw_output):,}-char exec output to {spill_path}; "
        "inspect specific lines with bash/sed/grep if needed] ...\n"
        if spilled
        else f"\n... [truncated {len(raw_output):,}-char exec output] ...\n"
    )
    avail = max(400, inline_cap - len(marker))
    head_len = min(EXEC_SPILL_HEAD_CHARS, int(avail * 0.62))
    tail_len = max(100, avail - head_len)
    return raw_output[:head_len] + marker + raw_output[-tail_len:]


def _image_dedup_key(resolved: str, data: bytes) -> Tuple[Any, ...]:
    """Build dedup key `(resolved_path, mtime_ns, size, sha16)` for active `view_image` tracking."""
    sha = hashlib.sha256(data).hexdigest()[:16]
    try:
        st = os.stat(resolved)
        mtime_ns: Optional[int] = int(st.st_mtime_ns)
        fsize = int(st.st_size)
    except OSError:
        mtime_ns = None
        fsize = len(data)
    return (resolved, mtime_ns, fsize, sha)


def _unique_name(name: str, taken: Any) -> str:
    """Clamp function name to 64 chars and disambiguate collisions."""
    candidate, n = name[:64], 1
    while candidate in taken:
        n += 1
        candidate = f"{name[:63 - len(str(n))]}_{n}"
    return candidate


def _resolve_path(session: taskbridge.Session, path: Any) -> str:
    """Resolve `path` against `session.workdir` when relative."""
    if not isinstance(path, str) or not path:
        raise taskbridge.TaskError("path must be a non-empty string")
    return path if path.startswith("/") else posixpath.join(session.workdir, path)


class MCPServer(taskbridge.MCPClient):
    """Streamable-HTTP MCP client wrapper exposing container MCP tools."""

    def __init__(self, session: taskbridge.Session, spec: Dict[str, Any]) -> None:
        super().__init__(session, str(spec.get("url") or ""), client="ple-submission")
        self.name = str(spec.get("name") or "mcp")
        self.tools: List[Dict[str, Any]] = []

    async def start(self) -> None:
        """Run MCP initialize handshake and fetch available tool definitions."""
        await self.initialize()
        self.tools = await self.list_tools()

    async def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """Invoke MCP tool `name` and return formatted text output."""
        result = await self.call_tool(name, arguments)
        parts: List[str] = []
        raw_content = result.get("content")
        content = raw_content if isinstance(raw_content, list) else []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text", "")))
            elif kind == "resource":
                parts.append(json.dumps(item.get("resource"))[:RESULT_CHARS])
            elif kind == "resource_link":
                parts.append(json.dumps(item)[:RESULT_CHARS])
            else:
                parts.append(f"[{kind or 'unknown'} content not shown]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"]))
        text = "\n".join(parts) or "(no content)"
        return f"error: {text}" if result.get("isError") else text


async def connect_mcp(
    session: taskbridge.Session,
) -> Tuple[Dict[str, Tuple[MCPServer, Dict[str, Any]]], List[Dict[str, Any]], str]:
    """Connect to all declared task MCP servers and return `(registry, tool_defs, note)`."""
    registry: Dict[str, Tuple[MCPServer, Dict[str, Any]]] = {}
    for spec in session.mcp_servers:
        server = MCPServer(session, spec)
        try:
            await server.start()
        except (taskbridge.TaskError, taskbridge.MCPError, RuntimeError, ValueError) as exc:
            await session.log(f"MCP server {server.name} unavailable: {exc}")
            continue
        for t in server.tools:
            clean = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server.name}__{t['name']}")
            registry[_unique_name(clean, registry)] = (server, t)
        await session.log(f"MCP server {server.name}: {len(server.tools)} tool(s)")

    if not registry:
        return registry, [], ""

    functions: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (t.get("description") or t["name"])[:1024],
                "parameters": (
                    {
                        k: v
                        for k, v in (t.get("inputSchema") or {}).items()
                        if k != "$schema"
                    }
                    or {"type": "object", "properties": {}}
                ),
            },
        }
        for name, (_, t) in registry.items()
    ]
    if (
        len(json.dumps(functions)) <= DIRECT_MCP_CHARS
        and len(TOOLS) + len(functions) <= MAX_TOOLS
    ):
        return (
            registry,
            functions,
            "\n\nThe task also exposes MCP tools (<server>__<tool>).",
        )

    servers: Dict[str, List[str]] = {}
    for name, (server, _) in registry.items():
        servers.setdefault(server.name, []).append(name)
    catalog = "\n".join(f"{srv}: {', '.join(names)}" for srv, names in servers.items())
    return (
        registry,
        list(MCP_CATALOG_TOOLS),
        "\n\nMCP tools available (use mcp_find then mcp_call):\n" + catalog,
    )


def find_mcp(registry: Dict[str, Tuple[MCPServer, Dict[str, Any]]], query: str) -> str:
    """Search MCP registry tools matching `query`."""
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]

    def _score(item: Tuple[str, Tuple[MCPServer, Dict[str, Any]]]) -> int:
        name, (_, t) = item
        hay = f"{name} {t.get('description') or ''}".lower()
        return (
            int(query == name) * 100
            + sum(w in name.lower() for w in words) * 3
            + sum(w in hay for w in words)
        )

    matches = [
        item
        for item in sorted(registry.items(), key=_score, reverse=True)[:8]
        if _score(item) > 0
    ]
    if not matches:
        return f"error: no MCP tool matches {query!r}"
    formatted = [
        f"{name}: {(t.get('description') or '')[:400]}\nparameters: {json.dumps(t.get('inputSchema') or {})}"
        for name, (_, t) in matches
    ]
    while len(formatted) > 1 and len("\n\n".join(formatted)) > RESULT_CHARS:
        formatted.pop()
    return "\n\n".join(formatted)


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"{constant} is not valid JSON")


def _extract_fn(call: Any) -> Dict[str, Any]:
    """Normalize a serialized or attribute-based tool call into a mutable function dict."""
    if isinstance(call, dict):
        fn = call.get("function")
        if isinstance(fn, dict):
            return fn
        return call
    fn = getattr(call, "function", None)
    if fn is not None:
        return {
            "name": getattr(fn, "name", ""),
            "arguments": getattr(fn, "arguments", "{}"),
        }
    return {"name": "", "arguments": "{}"}


async def edit_file_in_session(
    session: taskbridge.Session,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    """Replace exact text inside `path` in the task container."""
    if not isinstance(old_text, str) or not old_text:
        return "error: old_text must be a non-empty string"
    if not isinstance(new_text, str):
        return "error: new_text must be a string"
    resolved = _resolve_path(session, path)
    text = (await session.read_file(resolved)).decode("utf-8")
    count = text.count(old_text)
    if count == 0:
        return f"error: old_text was not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"error: old_text occurs {count} times in {path}; add context or "
            "set replace_all=true"
        )
    updated = text.replace(old_text, new_text, -1 if replace_all else 1)
    await session.write_file(resolved, updated)
    return f"replaced {count if replace_all else 1} occurrence(s) in {path}"


async def run_tool(
    session: taskbridge.Session,
    fn: Dict[str, Any],
    mcp_registry: Optional[Dict[str, Tuple[MCPServer, Dict[str, Any]]]] = None,
    image_sink: Optional[List[Dict[str, Any]]] = None,
    active_image_keys: Optional[set] = None,
) -> str:
    """Execute one model tool call inside the task container safely."""
    name = str(fn.get("name") or "").strip()
    raw_args = fn.get("arguments")
    try:
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            args = json.loads(raw_args or "{}", parse_constant=_reject_json_constant)
        if not isinstance(args, dict):
            raise TypeError("not a JSON object")
        fn["arguments"] = (
            json.dumps(args, ensure_ascii=False, allow_nan=False)
            .encode("utf-8", "replace")
            .decode("utf-8")
        )
    except (TypeError, ValueError, RecursionError) as exc:
        fn["arguments"] = "{}"
        return f"error: the arguments are not a JSON object ({exc}); send the call again"

    registry = mcp_registry or {}
    try:
        if name == "mcp_call":
            target_tool = str(args.get("tool") or "")
            target_args = args.get("arguments") or {}
            if isinstance(target_args, str):
                target_args = json.loads(target_args, parse_constant=_reject_json_constant)
            if not isinstance(target_args, dict):
                return "error: mcp_call arguments must be a JSON object"
            if target_tool not in registry:
                return f"error: no MCP tool named {target_tool!r}; use mcp_find"
            server, t = registry[target_tool]
            return clip(await server.call(t["name"], target_args))

        if name == "mcp_find":
            return find_mcp(registry, str(args.get("query") or ""))

        if name in registry:
            server, t = registry[name]
            return clip(await server.call(t["name"], args))

        if name == "bash":
            command = args["command"]
            if not isinstance(command, str) or not command:
                return "error: command must be a non-empty string"
            cap = _safe_int(session.limits.get("exec_timeout_sec"), default=600, minimum=1)
            timeout_sec = taskbridge.positive_int(args.get("timeout_sec", 120), 120, cap)
            result = await session.exec(command, timeout_sec=timeout_sec)
            note = " (timed out)" if result.get("timed_out") else ""
            formatted = f"exit code {result['exit_code']}{note}\n{result.get('output') or ''}"
            return await _format_or_spill_exec_output(session, clip(formatted))

        if name == "read_file":
            resolved = _resolve_path(session, args["path"])
            data = await session.read_file(resolved)
            if b"\0" in data[:8192]:
                mime = _detect_image_mime(data)
                if mime:
                    return (
                        f"{args['path']} is an image file ({mime}, {len(data)} bytes); "
                        "call view_image to inspect it visually"
                    )
                return (
                    f"{args['path']} is a binary file ({len(data)} bytes); "
                    "inspect it with bash"
                )
            return clip(data.decode("utf-8", "replace") or "(empty file)")

        if name == "view_image":
            resolved = _resolve_path(session, args["path"])
            data = await session.read_file(resolved)
            if not data:
                return f"error: {args['path']} is empty (0 bytes)"
            if len(data) > MAX_IMAGE_BYTES:
                return (
                    f"error: {args['path']} is {len(data)} bytes (exceeds {MAX_IMAGE_BYTES} byte "
                    "limit for view_image); crop or downscale it to PNG under /tmp first"
                )
            mime = _detect_image_mime(data)
            if not mime:
                return (
                    f"error: {args['path']} is not a supported image (PNG, JPEG, GIF, or WEBP)"
                )
            img_key = _image_dedup_key(resolved, data)
            if active_image_keys is not None and img_key in active_image_keys:
                return (
                    f"INFO: image {args['path']} ({mime}, {len(data)} bytes) is already loaded and "
                    "active in context above; reuse the already-attached visual inspection."
                )
            b64 = base64.b64encode(data).decode("ascii")
            if image_sink is not None:
                image_sink.append(
                    {
                        "path": args["path"],
                        "mime": mime,
                        "b64": b64,
                        "size": len(data),
                        "key": img_key,
                    }
                )
            if active_image_keys is not None:
                active_image_keys.add(img_key)
            return (
                f"loaded image {args['path']} ({mime}, {len(data)} bytes); "
                "attached visually below"
            )

        if name == "write_file":
            resolved = _resolve_path(session, args["path"])
            content = args["content"]
            append = bool(args.get("append", False))
            size = await session.write_file(resolved, content, append=append)
            return f"wrote {size} bytes to {args['path']}"

        if name == "edit_file":
            return await edit_file_in_session(
                session,
                args["path"],
                args["old_text"],
                args["new_text"],
                replace_all=bool(args.get("replace_all", False)),
            )

        return f"error: there is no tool named {name}"
    except (taskbridge.TaskError, taskbridge.MCPError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        return f"error: {type(exc).__name__}: {exc}"


async def shorten(session: taskbridge.Session, messages: List[Dict[str, Any]]) -> bool:
    """Truncate old images or older long tool results to recover from context overflow."""
    pruned_images = _prune_old_images(messages, max_images=1) or _prune_old_images(
        messages, max_images=0
    )
    if pruned_images:
        await session.log(f"context full: pruned {pruned_images} old image(s)")
        return True
    long_indices = [
        n
        for n, m in enumerate(messages)
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 2 * CUT_KEEP
    ]
    if not long_indices:
        return False
    cut = long_indices[: max(1, len(long_indices) // 2)]
    for n in cut:
        messages[n]["content"] = (
            str(messages[n]["content"])[:CUT_KEEP]
            + "\n[... cut to save context; run the tool again if needed]"
        )
    await session.log(f"context full: cut {len(cut)} old tool results")
    return True


async def recover(
    session: taskbridge.Session,
    messages: List[Dict[str, Any]],
    exc: Exception,
    waits: Iterator[int],
    until: float,
    call_opts: Optional[Dict[str, Any]] = None,
) -> bool:
    """Handle context overflow, unsupported parameter 400s, and transient API errors."""
    status = getattr(exc, "status_code", None)
    err_text = str(exc)
    if status == 400:
        if CONTEXT_FULL.search(err_text):
            return await shorten(session, messages)
        if call_opts is not None:
            if (
                call_opts.get("token_param") == "max_completion_tokens"
                and "max_completion_tokens" in err_text
                and UNSUPPORTED.search(err_text)
            ):
                call_opts["token_param"] = "max_tokens"
                return True
            if call_opts.get("effort") is not None and re.search(
                r"reasoning[._]effort", err_text
            ):
                call_opts["effort"] = None
                return True
    wait = next(waits, None)
    if wait is None or not (
        status is None or status in (408, 409, 429) or status >= 500
    ):
        return False
    if status in (429, 503):
        headers = getattr(getattr(exc, "response", None), "headers", None)
        ra_raw = None
        if isinstance(headers, dict):
            for hk, hv in headers.items():
                if str(hk).lower() == "retry-after":
                    ra_raw = hv
                    break
        elif hasattr(headers, "get"):
            ra_raw = headers.get("retry-after") or headers.get("Retry-After")
        if ra_raw is not None and not isinstance(ra_raw, bool):
            try:
                ra_val = float(ra_raw)
                if math.isfinite(ra_val) and ra_val > 0.0:
                    wait = max(wait, min(120.0, ra_val))
            except (TypeError, ValueError):
                pass
    left = session.time_left()
    if left is not None and left > until:
        wait = min(wait, max(0.0, left - until))
    if call_opts is not None:
        ring: List[str] = []
        for cand in [call_opts.get("primary_model") or call_opts.get("model")] + list(
            call_opts.get("fallback_models") or []
        ):
            c_str = str(cand or "").strip()
            if c_str and c_str not in ring:
                ring.append(c_str)
        if len(ring) > 1:
            next_idx = (int(call_opts.get("model_idx", 0)) + 1) % len(ring)
            prev_m = call_opts.get("model")
            call_opts["model_idx"] = next_idx
            call_opts["model"] = ring[next_idx]
            await session.log(f"rotating fallback model: {prev_m} -> {ring[next_idx]}")
    await session.log(
        f"model call failed ({err_text[:200]}); retrying in {wait:.0f} s"
    )
    await asyncio.sleep(wait)
    return True


_MCP_WRITE_RE = re.compile(
    r"(?:^|_)(create|add|update|edit|write|post|put|upload|set|delete|remove|close|assign|send|reply|append|patch|upsert|comment|note)(?:_|$)",
    re.I,
)
_MCP_READ_RE = re.compile(
    r"(?:^|_)(get|list|read|fetch|search|find|query|view|show|inspect|download|info|relations|comments|journals|activities|history|pages|files|statuses)(?:_|$)",
    re.I,
)
_MCP_COMMENT_WRITE_RE = re.compile(
    r"(?:comment|note|journal|reply|update_work_package|create_work_package|update_issue)",
    re.I,
)
_MCP_COMMENT_READ_RE = re.compile(
    r"(?:comment|journal|activit|note|history|replies)",
    re.I,
)
_MCP_ACTION_SPLIT_RE = re.compile(
    r"_(?:create|add|update|edit|write|post|put|upload|set|delete|remove|close|"
    r"assign|send|reply|append|patch|upsert|get|list|read|fetch|search|find|"
    r"query|view|show|inspect|download|manage)(?:_|$)",
    re.I,
)
_META_STATUS_LINE_RE = re.compile(
    r"\b(?:whether|one way or the other|yes/no|pass/fail|pass or fail|"
    r"approved/rejected|eligible/ineligible)\b"
    r"|\b([a-z]{3,20})\s{0,3}(?:/|or)\s{0,3}(?:not\s{1,3}|non-)\1\b",
    re.I,
)
_NEG_STATUS_RE = re.compile(
    r"\b(?:not\s{1,3}|non[-_\s]{1,3})([a-z][a-z0-9_-]{2,24})\b",
    re.I,
)
_POS_WORD_RE = re.compile(r"\b([a-z][a-z0-9_-]{2,24})\b", re.I)
_CONTRADICTION_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "have", "has", "been", "being", "will", "would", "could", "should", "can",
        "may", "might", "must", "shall", "yet", "still", "any", "all", "one", "two",
        "date", "note", "call", "only", "also", "more", "than", "each", "both",
        "into", "onto", "upon", "under", "over", "per", "via", "due",
    }
)
_ANTONYM_STATUS_PAIRS: Tuple[Tuple[re.Pattern[str], re.Pattern[str], str], ...] = (
    (re.compile(r"\bpass(?:ed)?\b", re.I), re.compile(r"\bfail(?:ed|ing)?\b", re.I), "pass vs fail"),
    (re.compile(r"\bapproved\b", re.I), re.compile(r"\b(?:rejected|denied)\b", re.I), "approved vs rejected/denied"),
    (re.compile(r"\beligible\b", re.I), re.compile(r"\bineligible\b", re.I), "eligible vs ineligible"),
    (re.compile(r"\bcompliant\b", re.I), re.compile(r"\bnon[- ]?compliant\b", re.I), "compliant vs non-compliant"),
    (re.compile(r"\bin[- ]?scope\b", re.I), re.compile(r"\bout[- ]?of[- ]?scope\b", re.I), "in-scope vs out-of-scope"),
    (re.compile(r"\bvalid\b", re.I), re.compile(r"\binvalid\b", re.I), "valid vs invalid"),
    (re.compile(r"\baccepted\b", re.I), re.compile(r"\brejected\b", re.I), "accepted vs rejected"),
    (re.compile(r"\bactive\b", re.I), re.compile(r"\bwithdrawn\b", re.I), "active vs withdrawn"),
)
_CONSTRAINT_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9._/-]+")
_CONSTRAINT_PATH_EXTS = (
    ".docx", ".pdf", ".xlsx", ".xlsm", ".csv", ".tsv", ".ifc",
    ".json", ".yaml", ".yml", ".md", ".txt", ".py", ".png", ".jpg", ".jpeg",
)
_CONSTRAINT_THRESHOLD_RE = re.compile(
    r"(?:(?:>=|<=|!=|==|[<>±])\s{0,2}|(?<![A-Za-z0-9.]))"
    r"\d{1,10}(?:[.,]\d{1,6})?"
    r"(?:\s{0,2}-\s{0,2}\d{1,10}(?:[.,]\d{1,6})?)?\s{0,2}"
    r"(?:mm|cm|m\^2|m2|m|ft|in|kw|kva|mw|kv|hz|ms|deg|°c|°f|psi|bar|pa|db|rpm|s|%|aff)(?![A-Za-z0-9])",
    re.I,
)
_CONSTRAINT_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{4}/\d{2}/\d{2})\b"
)
_CONSTRAINT_ENTITY_RE = re.compile(
    r"\b(cable[_ -]?tray|busway|bus[_ -]?duct|chw[_ -]?pipe|sprinkler|pdu|conduit|duct|"
    r"inverter|array|checklists?|work[_ -]?package|register|panel|breaker|transformer|"
    r"building|road|ifc|csv|xlsx|docx|pdf|openproject|nextcloud|wiki)\b",
    re.I,
)
_EMPTY_TRIGGER_LINE_RE = re.compile(
    r"\b(empty|no entry|zero|none|as of|as the record stands|not yours to make|"
    r"stays with|do not release|pending signature|withdrawn|out of scope|must not|"
    r"never|prohibit|mandatory|releasable)\b|\[h\s{0,3}=\s{0,3}1\]",
    re.I,
)
_CONSTRAINT_REQ_ID_RANGE_RE = re.compile(
    r"\b([RHS])(\d{2,3})\s{0,2}(?:\.\.|[-–—]|\bto\b|\bthrough\b)\s{0,2}\1(\d{2,3})\b"
)
_CONSTRAINT_HYPHEN_ID_RANGE_RE = re.compile(
    r"\b(R-\d{2,6}-)(\d{1,4})\s{0,2}(?:\.\.|\bto\b|\bthrough\b)\s{0,2}(?:\1)?(\d{1,4})\b"
    r"|\b(R-\d{2,6}-)(\d{1,4})\s{0,2}[-–—]\s{0,2}\4(\d{1,4})\b"
)
_CONSTRAINT_REQ_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:R-\d{2,6}-\d{1,4}|R-[A-Z]{2,12}(?:-\d{1,4})?|(?<!-)[RHS]\d{2,3}(?!-\d))(?![A-Za-z0-9_])"
)
_CONSTRAINT_NON_REQ_CONTEXT_RE = re.compile(
    r"\b(?:insulation|thermal|r-value|refrigerant|aashto|truck|schedule|sch\.?|radius|fillet|bend|resistor)\b",
    re.I,
)
_CONSTRAINT_CAP_RE = re.compile(
    r"(?:<=|>=|≤|≥|\b(?:max(?:imum)?|min(?:imum)?|at\s{1,2}most|at\s{1,2}least|"
    r"no\s{1,2}more\s{1,2}than|not\s{1,2}to\s{1,2}exceed|cap(?:ped)?\s{1,2}at|target)\b(?:\s{1,2}of)?)"
    r"\s{0,2}\$?\d{1,10}(?:[.,]\d{1,6})?"
    r"(?:\s{0,2}(?:%|mm|cm|m/s|m\^2|m2|m|ft|in|kw|kva|mw|kv|v|a|hz|ms|deg|"
    r"°c|°f|psi|kpa|bar|pa|db|rpm|gpm|cfm|fps|psf|plf|kip|kips|klf|lbs|lb|usd|\$))?"
    r"(?![A-Za-z0-9])",
    re.I,
)
_REGISTER_ID_TOKEN_RE = re.compile(r"^[A-Z]{1,6}(?:-[A-Z0-9]{1,8}){0,3}$")


def _extract_id_token(item: Any) -> str:
    """Extract a clean ID string from a string or dict entry."""
    if isinstance(item, dict):
        for key in ("id", "code", "requirement_id", "register_id"):
            val = item.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""
    if item is None:
        return ""
    return str(item).strip()


def _extract_req_ids(text: str, limit: int = 120) -> List[str]:
    """Deterministically extract requirement/finding/scenario IDs in linear time."""
    if not text:
        return []
    ids: List[str] = []
    for m in _CONSTRAINT_REQ_ID_RANGE_RE.finditer(text):
        prefix, s_str, e_str = m.group(1), m.group(2), m.group(3)
        start_n, end_n = int(s_str), int(e_str)
        if 0 <= end_n - start_n <= 100:
            width = max(len(s_str), len(e_str))
            for n in range(start_n, end_n + 1):
                ids.append(f"{prefix}{n:0{width}d}")
    for m in _CONSTRAINT_HYPHEN_ID_RANGE_RE.finditer(text):
        prefix = m.group(1) or m.group(4)
        s_str = m.group(2) or m.group(5)
        e_str = m.group(3) or m.group(6)
        start_n, end_n = int(s_str), int(e_str)
        if 0 <= end_n - start_n <= 100:
            width = max(len(s_str), len(e_str))
            for n in range(start_n, end_n + 1):
                ids.append(f"{prefix}{n:0{width}d}")
    for m in _CONSTRAINT_REQ_ID_RE.finditer(text):
        tok = m.group(0)
        if "-" not in tok:
            lo = max(0, m.start() - 25)
            hi = min(len(text), m.end() + 25)
            if _CONSTRAINT_NON_REQ_CONTEXT_RE.search(text[lo:hi]):
                continue
        ids.append(tok)
    return _dedupe_strs(ids, limit=limit)


def _extract_path_candidates(text: str) -> List[str]:
    """Extract file paths deterministically in linear time (no regex backtracking)."""
    out: List[str] = []
    for raw_tok in _CONSTRAINT_PATH_TOKEN_RE.findall(text):
        tok = raw_tok.rstrip(".")
        if not tok:
            continue
        low = tok.lower()
        if any(
            low.endswith(ext)
            and len(low) > len(ext)
            and low[-len(ext) - 1] not in "/."
            for ext in _CONSTRAINT_PATH_EXTS
        ):
            out.append(tok)
            continue
        if tok.startswith("/") and not tok.startswith("//"):
            if len([seg for seg in tok.split("/") if seg]) >= 2:
                out.append(tok)
    return out


def _dedupe_strs(items: Iterable[str], limit: int = 30) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in items:
        s = " ".join(str(raw).split()).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _regex_extract_constraints(task: str, plan: str = "") -> Dict[str, Any]:
    """Zero-latency deterministic fallback extractor for CONSTRAINTS.json."""
    text = f"{task or ''}\n{plan or ''}".strip()
    file_paths = _dedupe_strs(_extract_path_candidates(text))
    required_register_ids = _extract_req_ids(text, limit=120)
    hard_numeric_caps = _dedupe_strs(_CONSTRAINT_CAP_RE.findall(text), limit=40)
    numeric_thresholds = _dedupe_strs(_CONSTRAINT_THRESHOLD_RE.findall(text))
    iso_dates = _dedupe_strs(_CONSTRAINT_DATE_RE.findall(text))
    raw_entities = _CONSTRAINT_ENTITY_RE.findall(text)
    entities = _dedupe_strs([re.sub(r"[\s-]+", "_", e.lower()) for e in raw_entities])
    triggers: List[str] = []
    for raw_line in re.split(r"[\r\n]+|(?<=[.!?])\s+", text):
        line = " ".join(raw_line.split()).strip()
        if line and _EMPTY_TRIGGER_LINE_RE.search(line):
            triggers.append(line[:220])
    empty_set_triggers = _dedupe_strs(triggers, limit=20)
    out: Dict[str, Any] = {
        "source": "regex_fallback",
        "file_paths": file_paths,
        "required_register_ids": required_register_ids,
        "hard_numeric_caps": hard_numeric_caps,
        "numeric_thresholds": numeric_thresholds,
        "iso_dates": iso_dates,
        "entities": entities,
        "empty_set_triggers": empty_set_triggers,
    }
    if not (
        file_paths
        or required_register_ids
        or hard_numeric_caps
        or numeric_thresholds
        or iso_dates
        or entities
        or empty_set_triggers
    ):
        out["note"] = "no_constraints_found"
    return out


def _parse_or_fallback_constraints(raw: str, task: str, plan: str) -> str:
    """Parse LLM CONSTRAINTS.json reply, falling back to regex extraction on failure."""
    payload = extract_json_payload(raw)
    if payload:
        try:
            parsed = json.loads(payload, parse_constant=_reject_json_constant)
            if isinstance(parsed, dict) and parsed:
                parsed.setdefault("source", "llm_extractor")
                text = f"{task or ''}\n{plan or ''}".strip()
                if text:
                    regex_ids = _extract_req_ids(text, limit=120)
                    existing_raw_ids = parsed.get("required_register_ids")
                    existing_ids = (
                        [_extract_id_token(item) for item in existing_raw_ids]
                        if isinstance(existing_raw_ids, list)
                        else ([_extract_id_token(existing_raw_ids)] if existing_raw_ids else [])
                    )
                    existing_ids = [x for x in existing_ids if x]
                    if regex_ids or existing_ids or "required_register_ids" in parsed:
                        parsed["required_register_ids"] = _dedupe_strs(
                            existing_ids + regex_ids,
                            limit=160,
                        )
                    regex_caps = _dedupe_strs(_CONSTRAINT_CAP_RE.findall(text), limit=40)
                    if regex_caps:
                        existing_caps_raw = parsed.get("hard_numeric_caps")
                        existing_caps = (
                            [str(x) for x in existing_caps_raw if str(x).strip()]
                            if isinstance(existing_caps_raw, list)
                            else ([str(existing_caps_raw)] if isinstance(existing_caps_raw, str) and existing_caps_raw.strip() else [])
                        )
                        parsed["hard_numeric_caps"] = _dedupe_strs(
                            existing_caps + regex_caps,
                            limit=60,
                        )
                return json.dumps(parsed, indent=2, ensure_ascii=False)[:12_000]
        except Exception:
            pass
    return json.dumps(_regex_extract_constraints(task, plan), indent=2, ensure_ascii=False)


def _extract_required_register_ids(
    constraints_json: str,
    task: str = "",
    plan: str = "",
) -> List[str]:
    """Collect deduplicated required IDs from CONSTRAINTS.json and task/plan text."""
    collected: List[str] = []
    if constraints_json:
        try:
            parsed = json.loads(constraints_json, parse_constant=_reject_json_constant)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            raw_ids = parsed.get("required_register_ids")
            items = raw_ids if isinstance(raw_ids, list) else ([raw_ids] if raw_ids else [])
            for item in items:
                tok = _extract_id_token(item).upper()
                if tok and (any(ch.isdigit() for ch in tok) or tok.startswith("R-")) and _REGISTER_ID_TOKEN_RE.match(tok):
                    collected.append(tok)
        collected.extend(_extract_req_ids(constraints_json, limit=160))
    if task or plan:
        collected.extend(_extract_req_ids(f"{task}\n{plan}", limit=160))
    return _dedupe_strs(collected, limit=200)


_AUDITABLE_DELIVERABLE_EXTS = (".xlsx", ".xlsm", ".csv", ".tsv", ".ifc", ".json", ".zip")


def _extract_target_deliverables(
    constraints_json: str,
    workdir: str = "",
) -> List[str]:
    """Extract normalized declared deliverable file paths strictly from CONSTRAINTS.json deliverables."""
    if not constraints_json:
        return []
    try:
        parsed = json.loads(constraints_json, parse_constant=_reject_json_constant)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_delivs = parsed.get("deliverables")
    items = raw_delivs if isinstance(raw_delivs, list) else ([raw_delivs] if raw_delivs else [])
    norm_wd = workdir.replace("\\", "/").strip().rstrip("/") if workdir else ""
    out: List[str] = []
    for item in items:
        raw_val = ""
        if isinstance(item, str):
            raw_val = item
        elif isinstance(item, dict):
            for key in ("path", "file", "filename", "target"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    raw_val = val
                    break
        if not raw_val:
            continue
        p = raw_val.strip().strip("`'\"")
        p = p.replace("\\", "/").strip()
        if norm_wd and p.lower().startswith(norm_wd.lower() + "/"):
            p = p[len(norm_wd) + 1 :]
        elif p.lower().startswith("/app/"):
            p = p[len("/app/") :]
        p = p.lstrip("./").lstrip("/")
        if not p or " " in p:
            continue
        if any(p.lower().endswith(ext) for ext in _AUDITABLE_DELIVERABLE_EXTS):
            out.append(p)
    return _dedupe_strs(out, limit=100)


_FORBIDDEN_STD_CLAUSE_RE = re.compile(
    r"\b(?:no|not|never|do\s+not|must\s+not|shall\s+not|prohibit\w*|forbid\w*|without|exclud\w*|ban(?:ned)?|omit)\b"
    r"[^\n.;]{0,70}?\b(UFC|Unified\s+Facilities\s+Criteria|UFGS|MIL-STD-\d+[A-Z]?|"
    r"NFPA\s+\d+[A-Z]?|IEEE\s+[A-Z0-9.-]+|ASHRAE\s+[0-9.]+)\b",
    re.I,
)
_DISALLOWED_SEC_CLAUSE_RE = re.compile(
    r"(?:\b(?:no|not|never|do\s+not|must\s+not|disallowed|prohibit\w*|forbid\w*|exclud\w*|"
    r"not\s+part\s+of\s+the\s+project\s+manual|not\s+in\s+(?:the\s+)?project\s+manual)\b"
    r"[^\n.;]{0,70}?\bSection\s+(\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?)\b"
    r"|\bSection\s+(\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?)\b[^\n.;]{0,45}?"
    r"\b(?:is\s+)?(?:disallowed|prohibited|forbidden|excluded|not\s+part\s+of|not\s+permitted)\b)",
    re.I,
)
_ALLOWED_SEC_LINE_RE = re.compile(
    r"(?:allowed\s+(?:spec\w*\s+)?sections|project\s+manual\s+sections|only\s+cross[- ]reference[^\n]*sections)[^\n]*",
    re.I,
)
_SEC_CODE_RE = re.compile(r"\b(\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?)\b")


def extract_exclusion_rules(
    constraints_json: str,
    task: str = "",
    plan: str = "",
) -> Dict[str, List[str]]:
    """Derive task-specific forbidden citations, section rules, and scope exclusions dynamically."""
    forbidden_citations: List[str] = []
    allowed_sections: List[str] = []
    disallowed_sections: List[str] = []
    required_scope_exclusions: List[str] = []

    if constraints_json:
        try:
            parsed = json.loads(constraints_json, parse_constant=_reject_json_constant)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("forbidden_citations", "prohibited_citations", "excluded_citations"):
                val = parsed.get(key)
                if isinstance(val, list):
                    forbidden_citations.extend(str(x).strip() for x in val if str(x).strip())
                elif isinstance(val, str) and val.strip():
                    forbidden_citations.append(val.strip())
            for key in ("allowed_sections",):
                val = parsed.get(key)
                if isinstance(val, list):
                    allowed_sections.extend(" ".join(str(x).split()) for x in val if str(x).strip())
            for key in ("disallowed_sections", "excluded_sections", "prohibited_sections"):
                val = parsed.get(key)
                if isinstance(val, list):
                    disallowed_sections.extend(" ".join(str(x).split()) for x in val if str(x).strip())
            for key in ("scope_exclusions", "required_scope_exclusions"):
                val = parsed.get(key)
                if isinstance(val, list):
                    required_scope_exclusions.extend(str(x).strip() for x in val if str(x).strip())

    # Scan task, plan, and any raw hard_constraints strings inside constraints_json dynamically
    combined = "\n".join(p for p in (task, plan, constraints_json) if p)
    for m in _FORBIDDEN_STD_CLAUSE_RE.finditer(combined):
        tok = " ".join(m.group(1).split())
        if tok.lower() == "unified facilities criteria":
            forbidden_citations.append("UFC")
        else:
            forbidden_citations.append(tok)

    for m in _DISALLOWED_SEC_CLAUSE_RE.finditer(combined):
        sec = m.group(1) or m.group(2)
        if sec:
            disallowed_sections.append(" ".join(sec.split()))

    for m_line in _ALLOWED_SEC_LINE_RE.finditer(combined):
        for sec in _SEC_CODE_RE.findall(m_line.group(0)):
            allowed_sections.append(" ".join(sec.split()))

    return {
        "forbidden_citations": _dedupe_strs(forbidden_citations, limit=40),
        "allowed_sections": _dedupe_strs(allowed_sections, limit=80),
        "disallowed_sections": _dedupe_strs(disallowed_sections, limit=80),
        "required_scope_exclusions": _dedupe_strs(required_scope_exclusions, limit=60),
    }


def detect_contradictory_comment_statuses(text: str) -> List[str]:
    """Flag same-line contradictory/negated status pairs in external tracking comments."""
    if not text or not isinstance(text, str):
        return []
    findings: List[str] = []
    seen_lines: set[str] = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line or _META_STATUS_LINE_RE.search(line):
            continue
        norm_key = line.lower()
        if norm_key in seen_lines:
            continue
        matched_label: Optional[str] = None
        for neg_m in _NEG_STATUS_RE.finditer(line):
            tok = neg_m.group(1).lower()
            if tok in _CONTRADICTION_STOP_WORDS or len(tok) < 3:
                continue
            masked = _NEG_STATUS_RE.sub(" ", line)
            if re.search(rf"(?<![a-z0-9_-]){re.escape(tok)}(?![a-z0-9_-])", masked, re.I):
                matched_label = f"{tok} vs not/non-{tok}"
                break
        if matched_label is None:
            for pos_re, neg_re, pair_label in _ANTONYM_STATUS_PAIRS:
                if neg_re.search(line):
                    masked = neg_re.sub(" ", line)
                    if pos_re.search(masked):
                        matched_label = pair_label
                        break
        if matched_label is not None:
            seen_lines.add(norm_key)
            findings.append(f"contradictory status ({matched_label}) in comment line: {line[:180]}")
    return findings


def _mcp_write_target_key(tool_name: str, args: Dict[str, Any]) -> Tuple[str, str]:
    """Identify logical target for deduplicating superseded MCP writes."""
    if not isinstance(args, dict):
        return (tool_name, "")
    req = args.get("request") if isinstance(args.get("request"), dict) else args
    if not isinstance(req, dict):
        return (tool_name, "")
    for key in (
        "work_package_id", "issue_id", "page_id", "id", "path", "file_path", "target", "subject"
    ):
        val = req.get(key)
        if val is not None and str(val).strip():
            # Group comment/update writes onto same work_package_id when amending via a new comment
            kind = "comment" if _MCP_COMMENT_WRITE_RE.search(tool_name) else tool_name
            return (kind, f"{key}={str(val).strip()}")
    return (tool_name, "")


def _extract_comment_texts_from_args(args: Any) -> List[str]:
    texts: List[str] = []
    if not isinstance(args, dict):
        return texts
    for holder in (args, args.get("request") if isinstance(args.get("request"), dict) else None):
        if not isinstance(holder, dict):
            continue
        for key in ("comment", "note", "notes", "body", "description", "content", "message", "text"):
            val = holder.get(key)
            if isinstance(val, str) and val.strip():
                texts.append(val)
    return texts


def _mcp_service_prefix(tool_name: str) -> str:
    """Extract logical MCP service prefix (e.g. `apps__openproject_mcp_`) for scoped checks."""
    raw = str(tool_name or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(.+?_mcp_)", raw, re.I)
    if m:
        return m.group(1)
    am = _MCP_ACTION_SPLIT_RE.search(raw)
    if am and am.start() > 0:
        return raw[: am.start() + 1]
    if "__" in raw:
        return raw.split("__", 1)[0] + "__"
    if "_" in raw:
        return raw.split("_", 1)[0] + "_"
    return raw


_MCP_WRITE_VERB_RE = re.compile(
    r"(?:^|_)(?:create|add|update|edit|write|post|put|upload|set|delete|remove|close|assign|send|reply|append|patch|upsert)(?:_|$)",
    re.I,
)


def _mcp_is_comment_reader(tool_name: str) -> bool:
    """Return True only if `tool_name` is a read/list MCP tool for comments or journals."""
    return bool(
        _MCP_READ_RE.search(tool_name)
        and _MCP_COMMENT_READ_RE.search(tool_name)
        and not _MCP_WRITE_VERB_RE.search(tool_name)
    )


def check_mcp_readback_errors(
    active_mcp_writes: Dict[Any, Dict[str, Any]],
    verify_reads: Sequence[str],
    mcp_registry: Optional[Dict[str, Any]],
) -> List[str]:
    """Check per-service MCP readback coverage after WORKER/FIX writes external stores."""
    if not active_mcp_writes:
        return []
    reads = [str(r) for r in (verify_reads or []) if str(r).strip()]
    if not reads:
        return ["External MCP stores were modified, but VERIFIER performed no MCP readback."]

    written_svcs: set = set()
    comment_svcs: set = set()
    for wr in active_mcp_writes.values():
        tname = str((wr or {}).get("tool") or "")
        if not tname:
            continue
        svc = _mcp_service_prefix(tname)
        written_svcs.add(svc)
        if _MCP_COMMENT_WRITE_RE.search(tname):
            comment_svcs.add(svc)

    reg_keys = [str(k) for k in (mcp_registry or {})]
    errors: List[str] = []
    for svc in sorted(written_svcs):
        svc_reg = [k for k in reg_keys if _mcp_service_prefix(k) == svc]
        svc_reads = [r for r in reads if _mcp_service_prefix(r) == svc]
        svc_has_comment_reader = any(_mcp_is_comment_reader(k) for k in svc_reg)
        svc_has_any_reader = any(_MCP_READ_RE.search(k) and not _MCP_WRITE_VERB_RE.search(k) for k in svc_reg)
        if svc in comment_svcs and svc_has_comment_reader:
            if not any(_mcp_is_comment_reader(r) for r in svc_reads):
                errors.append(
                    f"External comments/journals were updated on '{svc}', but VERIFIER did not "
                    "call an MCP comment/journal readback tool for that service."
                )
        elif svc_has_any_reader and not svc_reads:
            errors.append(
                f"External MCP store '{svc}' was modified, but VERIFIER performed no get/list readback on that service."
            )
    return errors


def conversation(
    system: str,
    session: taskbridge.Session,
    prompt: str,
    mcp_note: str = "",
) -> List[Dict[str, Any]]:
    """Build initial `[system, user]` chat messages for a role."""
    return [
        {"role": "system", "content": system.format(workdir=session.workdir) + mcp_note},
        {"role": "user", "content": prompt},
    ]


async def agent(
    session: taskbridge.Session,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    until: float,
    *,
    client: Any,
    runtime_cfg: Dict[str, Any],
    mcp_registry: Optional[Dict[str, Tuple[MCPServer, Dict[str, Any]]]] = None,
    breaker: Optional[CircuitBreaker] = None,
    final: bool = False,
    max_turns: Optional[int] = None,
    wrap_up_message: Optional[Dict[str, Any]] = None,
    run_state: Optional[Dict[str, Any]] = None,
    retry_waits: Optional[Sequence[int]] = None,
) -> str:
    """Run tool-calling loop on `messages` until the model produces a final reply."""
    model = runtime_cfg["model"]
    max_output_tokens = int(runtime_cfg["max_output_tokens"])
    cache_mark = bool(runtime_cfg.get("cache_mark", False))
    call_opts: Dict[str, Any] = {
        "token_param": "max_completion_tokens",
        "effort": runtime_cfg.get("effort"),
        "primary_model": model,
        "model": model,
        "fallback_models": list(runtime_cfg.get("fallback_models") or []),
        "model_idx": 0,
    }
    tool_breaker = breaker or CircuitBreaker(max_failures=runtime_cfg.get("max_failures", 3))
    active_wrap = dict(wrap_up_message or WRAP_UP)
    wrap_up_injected = False
    if run_state is not None:
        run_state.clear()
        run_state.update(
            {
                "turns": 0,
                "turn_cap": False,
                "time_cap": False,
                "truncated": False,
                "tool_calls": [],
                "mcp_writes": [],
                "mcp_reads": [],
            }
        )

    waits_seq = RETRY_WAITS if retry_waits is None else retry_waits
    nudges, head, waits, turns = 0, "", iter(waits_seq), 0
    image_key_by_part_id: Dict[int, Tuple[Any, ...]] = {}
    last_failed_bash_sig: Optional[str] = None
    while True:
        if session.cancelled.is_set():
            return session.final_message or head
        left = session.time_left()
        time_cap = left is not None and left < until
        turn_cap = max_turns is not None and turns >= max_turns - 1
        wrap_up = time_cap or turn_cap
        if wrap_up and not wrap_up_injected:
            wrap_up_injected = True
            if run_state is not None:
                run_state["turn_cap"] = bool(turn_cap)
                run_state["time_cap"] = bool(time_cap)
                run_state["truncated"] = True
            if turn_cap and not time_cap:
                await session.log(f"step reached max_turns={max_turns}; wrapping up")
            messages.append(dict(active_wrap))

        if left is None:
            timeout: Optional[float] = None
        elif wrap_up:
            timeout = max(1.0, min(float(WRAP_UP_SEC), left))
        else:
            timeout = max(1.0, left - until)

        if turns > 0 and turns % PRUNE_CHECKPOINT_EVERY == 0:
            pruned_tools, saved_tool_chars = prune_old_tool_outputs(messages)
            if pruned_tools:
                await session.log(
                    f"context pruner: pruned {pruned_tools} old tool output(s) ({saved_tool_chars:,} chars)"
                )

        active_model = str(call_opts.get("model") or model)
        active_cache_mark = bool(cache_mark or ("claude" in active_model.lower()))
        create_kwargs: Dict[str, Any] = {
            "model": active_model,
            "messages": marked(messages, active_cache_mark),
            call_opts["token_param"]: max_output_tokens,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "none" if wrap_up else "auto"
        if call_opts.get("effort"):
            create_kwargs["reasoning_effort"] = call_opts["effort"]

        try:
            reply = await asyncio.wait_for(
                client.chat.completions.create(**create_kwargs),
                timeout,
            )
        except asyncio.TimeoutError:
            if wrap_up:
                return head or session.final_message
            continue
        except openai.APIError as exc:
            if await recover(session, messages, exc, waits, until, call_opts):
                continue
            raise

        waits = iter(RETRY_WAITS)
        turns += 1
        if run_state is not None:
            run_state["turns"] = turns
        if getattr(reply, "usage", None):
            usage = reply.usage
            details = getattr(usage, "prompt_tokens_details", None)
            await session.usage(
                active_model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(details, "cached_tokens", 0) or 0,
            )

        choices = getattr(reply, "choices", None) or []
        if not choices:
            await session.log("model returned empty choices; nudging/retrying")
            if nudges >= MAX_NUDGES:
                if run_state is not None:
                    run_state["truncated"] = True
                return head or session.final_message
            nudges += 1
            continue

        choice = choices[0]
        message = choice.message
        cut = getattr(choice, "finish_reason", None) == "length"
        msg_content = getattr(message, "content", None) or ""
        has_content = bool(msg_content.strip())
        text = head + msg_content
        if final and has_content:
            session.final_message = text

        turn = (
            message.model_dump(exclude_none=True)
            if hasattr(message, "model_dump")
            else {"role": "assistant", "content": msg_content or None}
        )
        tool_calls = getattr(message, "tool_calls", None) or []

        if wrap_up or not tool_calls:
            turn.pop("tool_calls", None)
            if has_content:
                messages.append(turn)
            if wrap_up or (has_content and not cut) or nudges == MAX_NUDGES:
                if run_state is not None and (cut or not has_content):
                    run_state["truncated"] = True
                return text
            nudges, head = nudges + 1, text
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your reply was cut off; continue where it stopped."
                        if cut
                        else "Continue, and finish with your answer."
                    ),
                }
            )
            continue

        nudges, head = 0, ""
        raw_calls = turn.get("tool_calls") if turn.get("tool_calls") is not None else tool_calls
        if isinstance(raw_calls, dict) or not isinstance(raw_calls, (list, tuple)):
            raw_calls = [raw_calls]
        normalized_calls: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for idx, call in enumerate(raw_calls):
            if isinstance(call, dict):
                raw_id = call.get("id")
                call_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else f"call_{turns}_{idx}"
                raw_fn = call.get("function")
                if isinstance(raw_fn, dict):
                    fn_args = raw_fn.get("arguments")
                    norm_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(raw_fn.get("name") or ""),
                            "arguments": "{}" if fn_args is None else fn_args,
                        },
                    }
                else:
                    norm_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "__malformed__", "arguments": "{bad"},
                    }
            else:
                raw_id = getattr(call, "id", None) if call is not None else None
                call_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else f"call_{turns}_{idx}"
                fn_obj = getattr(call, "function", None) if call is not None else None
                if fn_obj is not None:
                    fn_args = getattr(fn_obj, "arguments", "{}")
                    norm_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(getattr(fn_obj, "name", "") or ""),
                            "arguments": "{}" if fn_args is None else fn_args,
                        },
                    }
                else:
                    norm_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "__malformed__", "arguments": "{bad"},
                    }
            base_id = norm_call["id"]
            if base_id in seen_ids:
                suffix = idx
                cand_id = f"{base_id}_{suffix}"
                while cand_id in seen_ids:
                    suffix += 1
                    cand_id = f"{base_id}_{suffix}"
                norm_call["id"] = cand_id
            seen_ids.add(norm_call["id"])
            normalized_calls.append(norm_call)
        turn["tool_calls"] = normalized_calls
        messages.append(turn)

        serialized_calls = normalized_calls
        tripped = False
        pending_images: List[Dict[str, Any]] = []
        live_img_parts = [
            part
            for msg in messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list)
            for part in msg["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        live_part_ids = {id(p) for p in live_img_parts}
        image_key_by_part_id = {
            pid: key for pid, key in image_key_by_part_id.items() if pid in live_part_ids
        }
        active_image_keys: set = set(image_key_by_part_id.values())
        for idx, call in enumerate(serialized_calls):
            call_id = call["id"]
            fn = _extract_fn(call)
            await session.log(
                f"{fn.get('name', '')} {str(fn.get('arguments', ''))[:200]}"
            )
            fn_name_cur = str(fn.get("name") or "").strip()
            bash_sig: Optional[str] = None
            if fn_name_cur == "bash":
                try:
                    b_args = json.loads(str(fn.get("arguments") or "{}"))
                    b_cmd = " ".join(str(b_args.get("command") or "").split()) if isinstance(b_args, dict) else ""
                    if b_cmd:
                        bash_sig = hashlib.sha256(b_cmd.encode("utf-8", errors="replace")).hexdigest()[:16]
                except Exception:
                    bash_sig = None

            if bash_sig and bash_sig == last_failed_bash_sig:
                content = (
                    "error: identical failing bash command repeated without any intervening file edit; "
                    "inspect the previous error and modify your command or script before re-running."
                )
            else:
                content = await run_tool(
                    session,
                    fn,
                    mcp_registry=mcp_registry,
                    image_sink=pending_images,
                    active_image_keys=active_image_keys,
                )

            if fn_name_cur in ("write_file", "edit_file") and not content.startswith("error:"):
                last_failed_bash_sig = None
            elif fn_name_cur == "bash" and bash_sig:
                if content.startswith("exit code 0\n") or content == "exit code 0":
                    last_failed_bash_sig = None
                elif content.startswith("exit code ") and not content.startswith("exit code 0"):
                    last_failed_bash_sig = bash_sig
            if content.startswith("error:"):
                if tool_breaker.record_failure():
                    tripped = True
            else:
                # Any clean tool execution ends an active error streak and clears stale tripped state.
                tool_breaker.reset()
                tripped = False
                if run_state is not None:
                    fn_name = str(fn.get("name") or "")
                    actual_tool = ""
                    actual_args: Dict[str, Any] = {}
                    try:
                        parsed_fn_args = json.loads(str(fn.get("arguments") or "{}"))
                    except Exception:
                        parsed_fn_args = {}
                    if isinstance(parsed_fn_args, dict):
                        if fn_name == "mcp_call":
                            actual_tool = str(parsed_fn_args.get("tool") or "")
                            raw_targs = parsed_fn_args.get("arguments")
                            if isinstance(raw_targs, dict):
                                actual_args = raw_targs
                            elif isinstance(raw_targs, str):
                                try:
                                    dec = json.loads(raw_targs)
                                    if isinstance(dec, dict):
                                        actual_args = dec
                                except Exception:
                                    actual_args = {}
                        elif fn_name in (mcp_registry or {}):
                            actual_tool = fn_name
                            actual_args = parsed_fn_args
                    run_state.setdefault("tool_calls", []).append(actual_tool or fn_name)
                    if actual_tool:
                        if _MCP_WRITE_RE.search(actual_tool):
                            run_state.setdefault("mcp_writes", []).append(
                                {"tool": actual_tool, "arguments": actual_args}
                            )
                        elif _MCP_READ_RE.search(actual_tool):
                            run_state.setdefault("mcp_reads", []).append(actual_tool)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                }
            )

        now_left = session.time_left()
        if messages[-1].get("role") == "tool":
            if max_turns is not None:
                remaining_tool_turns = max(0, (max_turns - 1) - turns)
                time_suffix = (
                    f", {max(0, int((now_left - until) // 60))}m left"
                    if now_left is not None
                    else ""
                )
                pacing = (
                    f"\n[Turn {turns}/{max_turns} ({remaining_tool_turns} tool turn(s) "
                    f"left before wrap-up){time_suffix}]"
                )
                if final:
                    if max_turns >= 4 and 1 <= remaining_tool_turns <= 3:
                        pacing += (
                            f"\n[URGENT: only {remaining_tool_turns} tool turn(s) left "
                            "before forced wrap-up! Finalize, regenerate, and verify "
                            "every required deliverable file on disk right now.]"
                        )
                    elif (
                        max_turns >= 6
                        and turns >= max_turns // 2
                        and remaining_tool_turns > 3
                    ):
                        pacing += (
                            "\n[Pacing: past halfway through turn budget — write and "
                            "test a working version of every required deliverable file "
                            "on disk now if not already created.]"
                        )
                elif max_turns >= 4 and 1 <= remaining_tool_turns <= 2:
                    pacing += (
                        f"\n[Pacing: only {remaining_tool_turns} tool turn(s) left "
                        "before wrap-up — conclude concisely.]"
                    )
                messages[-1]["content"] += pacing
            elif now_left is not None:
                messages[-1]["content"] += (
                    f"\n[{max(0, int((now_left - until) // 60))} minutes left for this step]"
                )

        if pending_images:
            parts: List[Dict[str, Any]] = []
            for img in pending_images:
                parts.append(
                    {
                        "type": "text",
                        "text": (
                            f"Image from view_image({img['path']!r}) "
                            f"({img['mime']}, {img['size']} bytes):"
                        ),
                    }
                )
                img_part = {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img['mime']};base64,{img['b64']}"},
                }
                if img.get("key") is not None:
                    image_key_by_part_id[id(img_part)] = img["key"]
                parts.append(img_part)
            messages.append({"role": "user", "content": parts})
            _prune_old_images(messages, max_images=MAX_ACTIVE_IMAGES)
            live_after = {
                id(part)
                for msg in messages
                if msg.get("role") == "user" and isinstance(msg.get("content"), list)
                for part in msg["content"]
                if isinstance(part, dict) and part.get("type") == "image_url"
            }
            image_key_by_part_id = {
                pid: key for pid, key in image_key_by_part_id.items() if pid in live_after
            }

        if tripped and tool_breaker.failure_count > 0:
            await session.log(
                f"tool circuit breaker tripped after {tool_breaker.failure_count} "
                "consecutive tool errors; injecting recovery guidance"
            )
            tool_breaker.reset()
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Consecutive tool calls failed validation or execution. "
                        "Stop repeating the failing call, inspect the error messages, "
                        "and use a corrected command or path."
                    ),
                }
            )


class _DryRunObject:
    """Minimal OpenAI SDK response double supporting attribute access and model_dump."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, _DryRunObject(value))
            elif isinstance(value, list):
                setattr(
                    self,
                    key,
                    [_DryRunObject(item) if isinstance(item, dict) else item for item in value],
                )
            else:
                setattr(self, key, value)

    def model_dump(self, exclude_none: bool = True) -> Dict[str, Any]:
        """Serialize the mock response dict like Pydantic's BaseModel.model_dump."""
        dumped = copy.deepcopy(self._data)
        if exclude_none:
            return {k: v for k, v in dumped.items() if v is not None}
        return dumped


class DryRunOpenAIClient:
    """Offline mock AsyncOpenAI client exercising tools, MCP, and Plan-Work-Verify."""

    def __init__(self) -> None:
        self.chat = _DryRunChatNamespace(self)

    async def create_completion(self, **kwargs: Any) -> _DryRunObject:
        """Synthesize realistic multi-turn planner, worker, and verifier responses."""
        await asyncio.sleep(0)
        messages: List[Dict[str, Any]] = kwargs.get("messages") or []
        tools: List[Dict[str, Any]] = kwargs.get("tools") or []
        tool_choice = kwargs.get("tool_choice", "auto")

        sys_text = ""
        user_text = ""
        for msg in messages:
            if msg.get("role") == "system" and not sys_text:
                sys_text = str(msg.get("content") or "")
            elif msg.get("role") == "user" and not user_text:
                user_text = str(msg.get("content") or "")

        tool_results = [m for m in messages if m.get("role") == "tool"]
        called_names: List[str] = []
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    called_names.append(tc.get("function", {}).get("name", ""))

        def _reply(
            content: Optional[str],
            tool_calls: Optional[List[Dict[str, Any]]] = None,
            finish: str = "stop",
        ) -> _DryRunObject:
            msg_dict: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
                finish = "tool_calls"
            return _DryRunObject(
                {
                    "choices": [{"message": msg_dict, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 32,
                        "completion_tokens": 16,
                        "prompt_tokens_details": {"cached_tokens": 0},
                    },
                }
            )

        if tool_choice == "none":
            return _reply("Dry-run wrap-up summary completed.")

        lower_sys = sys_text.lower()
        target_name = "mcp.txt" if "mcp.txt" in user_text.lower() else "hello.txt"

        if "plan an engineering task" in lower_sys:
            if not tool_results:
                return _reply(
                    None,
                    [
                        {
                            "id": "dry_plan_1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps({"command": "pwd && ls -la"}),
                            },
                        }
                    ],
                )
            return _reply(
                f"1. Inspect workspace.\n2. Write deliverable `{target_name}`.\n3. Verify file."
            )

        if "extract strict engineering constraints" in lower_sys:
            return _reply(
                json.dumps(
                    {
                        "source": "llm_extractor",
                        "deliverables": [target_name],
                        "hard_constraints": [],
                        "numeric_thresholds": [],
                        "iso_dates": [],
                        "entities": [],
                    }
                )
            )

        if "check an engineer's work" in lower_sys:
            if not tool_results:
                return _reply(
                    None,
                    [
                        {
                            "id": "dry_verify_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": target_name}),
                            },
                        }
                    ],
                )
            return _reply("PASS")

        # Worker role
        mcp_funcs = [
            t["function"]
            for t in tools
            if isinstance(t, dict)
            and isinstance(t.get("function"), dict)
            and "__" in t["function"].get("name", "")
        ]
        if mcp_funcs and not any("__" in n for n in called_names):
            first_fn = mcp_funcs[0]
            props = (first_fn.get("parameters") or {}).get("properties") or {}
            sample_args: Dict[str, Any] = {}
            for key, schema in props.items():
                stype = schema.get("type") if isinstance(schema, dict) else "string"
                sample_args[key] = 1 if stype in ("integer", "number") else "ok"
            return _reply(
                None,
                [
                    {
                        "id": "dry_mcp_1",
                        "type": "function",
                        "function": {
                            "name": first_fn["name"],
                            "arguments": json.dumps(sample_args),
                        },
                    }
                ],
            )

        if "write_file" not in called_names:
            content = "HELLO\n"
            if target_name == "mcp.txt" and tool_results:
                last_mcp = str(tool_results[-1].get("content") or "").strip()
                content = f"{last_mcp.splitlines()[0]}\n" if last_mcp else "HELLO\n"
            return _reply(
                None,
                [
                    {
                        "id": "dry_write_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": target_name, "content": content}),
                        },
                    }
                ],
            )

        return _reply(f"Dry-run deliverable `{target_name}` written and verified.")


class _DryRunChatNamespace:
    def __init__(self, parent: DryRunOpenAIClient) -> None:
        self.completions = _DryRunCompletionsNamespace(parent)


class _DryRunCompletionsNamespace:
    def __init__(self, parent: DryRunOpenAIClient) -> None:
        self._parent = parent

    async def create(self, **kwargs: Any) -> _DryRunObject:
        return await self._parent.create_completion(**kwargs)


AUDIT_MARKER = "__PLE_AUDIT_JSON__:"
XLSX_BASELINE_MARKER = "__PLE_XLSX_BASELINE_JSON__:"
FILE_BASELINE_MARKER = "__PLE_FILE_BASELINE_JSON__:"
IFC_BASELINE_MARKER = "__PLE_IFC_BASELINE_JSON__:"

_XLSX_BASELINE_SCRIPT = r'''
import hashlib
import json
import os
import re
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "site-packages",
}
AUDIT_BASELINE_EXTS = {".xlsx", ".xlsm", ".csv", ".tsv", ".ifc", ".json", ".zip", ".docx", ".md"}
LARGE_BASELINE_EXTS = {".ifc", ".zip"}

root = Path.cwd()
files = []
for cur, dirs, names in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
    for name in sorted(names):
        if name.startswith("."):
            continue
        p = Path(cur) / name
        if p.is_file():
            files.append(p)
        if len(files) >= 2000:
            break
    if len(files) >= 2000:
        break

xlsx_baseline = {}
file_baseline = {}
ifc_gids = set()
_IFC_STEP_GID_RE = re.compile(
    r"#[0-9]+\s*=\s*IFC[A-Z0-9_]+\s*\(\s*'([0-9A-Za-z_$]{1,64})'",
    re.I,
)
try:
    import ifcopenshell as _ifc_base_mod
except Exception:
    _ifc_base_mod = None

for p in files:
    suf = p.suffix.lower()
    if suf not in AUDIT_BASELINE_EXTS:
        continue
    max_bytes = 50 * 1024 * 1024 if suf in LARGE_BASELINE_EXTS else 25 * 1024 * 1024
    try:
        size = p.stat().st_size
        if size <= max_bytes:
            h = hashlib.sha256()
            with p.open("rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            entry = [size, h.hexdigest()]
            rel = p.relative_to(root).as_posix()
            file_baseline[rel] = entry
            if suf in {".xlsx", ".xlsm"}:
                xlsx_baseline[rel] = entry
    except OSError:
        continue
    if suf == ".ifc" and size <= 50 * 1024 * 1024:
        try:
            if _ifc_base_mod is not None:
                try:
                    _m = _ifc_base_mod.open(str(p))
                    _file_gids = []
                    for _prod in _m.by_type("IfcProduct"):
                        _gid = str(getattr(_prod, "GlobalId", None) or "").strip()
                        if _gid:
                            _file_gids.append(_gid)
                    if _file_gids:
                        ifc_gids.update(_file_gids)
                        continue
                except Exception:
                    pass
            _txt = p.read_text(encoding="utf-8", errors="ignore")
            for _gm in _IFC_STEP_GID_RE.finditer(_txt):
                ifc_gids.add(_gm.group(1))
            if "/* SCENE_JSON:" in _txt:
                _s = _txt.split("/* SCENE_JSON:", 1)[1].split("*/", 1)[0]
                _scene = json.loads(_s)
                for _el in (_scene.get("elements") or []):
                    _gid = str(_el.get("gid") or "").strip()
                    if _gid:
                        ifc_gids.add(_gid)
        except Exception:
            continue

print("__PLE_XLSX_BASELINE_JSON__:" + json.dumps(xlsx_baseline))
print("__PLE_FILE_BASELINE_JSON__:" + json.dumps(file_baseline))
print("__PLE_IFC_BASELINE_JSON__:" + json.dumps(sorted(ifc_gids)[:50000]))
'''

_PROGRAMMATIC_AUDIT_SCRIPT = r'''
import csv
import hashlib
import inspect
import io
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

START_T = time.monotonic()
BASELINE_XLSX = {}  # __PLE_BASELINE_XLSX__
if not BASELINE_XLSX and os.environ.get("PLE_AUDIT_BASELINE_XLSX_JSON"):
    try:
        _env_base = json.loads(os.environ["PLE_AUDIT_BASELINE_XLSX_JSON"])
        if isinstance(_env_base, dict):
            BASELINE_XLSX = _env_base
    except Exception:
        BASELINE_XLSX = {}
BASELINE_FILES = {}  # __PLE_BASELINE_FILES__
if not BASELINE_FILES and os.environ.get("PLE_AUDIT_BASELINE_FILES_JSON"):
    try:
        _env_files = json.loads(os.environ["PLE_AUDIT_BASELINE_FILES_JSON"])
        if isinstance(_env_files, dict):
            BASELINE_FILES = _env_files
    except Exception:
        BASELINE_FILES = {}
if isinstance(BASELINE_XLSX, dict):
    for _bk, _bv in BASELINE_XLSX.items():
        BASELINE_FILES.setdefault(_bk, _bv)
BASELINE_IFC_GIDS = set()  # __PLE_BASELINE_IFC_GIDS__
if not BASELINE_IFC_GIDS and os.environ.get("PLE_AUDIT_BASELINE_IFC_GIDS_JSON"):
    try:
        _env_ifc = json.loads(os.environ["PLE_AUDIT_BASELINE_IFC_GIDS_JSON"])
        if isinstance(_env_ifc, list):
            BASELINE_IFC_GIDS = {str(x) for x in _env_ifc if str(x).strip()}
    except Exception:
        BASELINE_IFC_GIDS = set()
else:
    BASELINE_IFC_GIDS = {str(x) for x in BASELINE_IFC_GIDS if str(x).strip()}
REQUIRED_REGISTER_IDS = []  # __PLE_REQUIRED_REGISTER_IDS__
if not REQUIRED_REGISTER_IDS and os.environ.get("PLE_AUDIT_REQUIRED_REGISTER_IDS_JSON"):
    try:
        _env_rids = json.loads(os.environ["PLE_AUDIT_REQUIRED_REGISTER_IDS_JSON"])
        if isinstance(_env_rids, list):
            REQUIRED_REGISTER_IDS = [str(x).strip() for x in _env_rids if str(x).strip()]
    except Exception:
        REQUIRED_REGISTER_IDS = []
else:
    REQUIRED_REGISTER_IDS = [str(x).strip() for x in REQUIRED_REGISTER_IDS if str(x).strip()]
TARGET_DELIVERABLES = []  # __PLE_TARGET_DELIVERABLES__
if not TARGET_DELIVERABLES and os.environ.get("PLE_AUDIT_TARGET_DELIVERABLES_JSON"):
    try:
        _env_tdelivs = json.loads(os.environ["PLE_AUDIT_TARGET_DELIVERABLES_JSON"])
        if isinstance(_env_tdelivs, list):
            TARGET_DELIVERABLES = [str(x).strip() for x in _env_tdelivs if str(x).strip()]
    except Exception:
        TARGET_DELIVERABLES = []
else:
    TARGET_DELIVERABLES = [str(x).strip() for x in TARGET_DELIVERABLES if str(x).strip()]
FORBIDDEN_CITATIONS = []  # __PLE_FORBIDDEN_CITATIONS__
if not FORBIDDEN_CITATIONS and os.environ.get("PLE_AUDIT_FORBIDDEN_CITATIONS_JSON"):
    try:
        _env_forb = json.loads(os.environ["PLE_AUDIT_FORBIDDEN_CITATIONS_JSON"])
        if isinstance(_env_forb, list):
            FORBIDDEN_CITATIONS = [str(x).strip() for x in _env_forb if str(x).strip()]
    except Exception:
        FORBIDDEN_CITATIONS = []
else:
    FORBIDDEN_CITATIONS = [str(x).strip() for x in FORBIDDEN_CITATIONS if str(x).strip()]
ALLOWED_SECTIONS = []  # __PLE_ALLOWED_SECTIONS__
if not ALLOWED_SECTIONS and os.environ.get("PLE_AUDIT_ALLOWED_SECTIONS_JSON"):
    try:
        _env_asec = json.loads(os.environ["PLE_AUDIT_ALLOWED_SECTIONS_JSON"])
        if isinstance(_env_asec, list):
            ALLOWED_SECTIONS = [" ".join(str(x).split()) for x in _env_asec if str(x).strip()]
    except Exception:
        ALLOWED_SECTIONS = []
else:
    ALLOWED_SECTIONS = [" ".join(str(x).split()) for x in ALLOWED_SECTIONS if str(x).strip()]
DISALLOWED_SECTIONS = []  # __PLE_DISALLOWED_SECTIONS__
if not DISALLOWED_SECTIONS and os.environ.get("PLE_AUDIT_DISALLOWED_SECTIONS_JSON"):
    try:
        _env_dsec = json.loads(os.environ["PLE_AUDIT_DISALLOWED_SECTIONS_JSON"])
        if isinstance(_env_dsec, list):
            DISALLOWED_SECTIONS = [" ".join(str(x).split()) for x in _env_dsec if str(x).strip()]
    except Exception:
        DISALLOWED_SECTIONS = []
else:
    DISALLOWED_SECTIONS = [" ".join(str(x).split()) for x in DISALLOWED_SECTIONS if str(x).strip()]
REQUIRED_SCOPE_EXCLUSIONS = []  # __PLE_REQUIRED_SCOPE_EXCLUSIONS__
if not REQUIRED_SCOPE_EXCLUSIONS and os.environ.get("PLE_AUDIT_SCOPE_EXCLUSIONS_JSON"):
    try:
        _env_excl = json.loads(os.environ["PLE_AUDIT_SCOPE_EXCLUSIONS_JSON"])
        if isinstance(_env_excl, list):
            REQUIRED_SCOPE_EXCLUSIONS = [str(x).strip() for x in _env_excl if str(x).strip()]
    except Exception:
        REQUIRED_SCOPE_EXCLUSIONS = []
else:
    REQUIRED_SCOPE_EXCLUSIONS = [str(x).strip() for x in REQUIRED_SCOPE_EXCLUSIONS if str(x).strip()]
ENFORCE_DELIVERABLES = False  # __PLE_ENFORCE_DELIVERABLES__
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "site-packages",
}
SKIP_JSON_NAMES = {"package.json", "package-lock.json", "tsconfig.json"}
FORMULA_ERRORS = {
    "#REF!",
    "#NAME?",
    "#VALUE!",
    "#DIV/0!",
    "#N/A",
    "#NUM!",
    "#NULL!",
    "#SPILL!",
    "#CALC!",
}
NAN_TOKENS = {"nan", "+nan", "-nan", "inf", "+inf", "-inf"}
PLACEHOLDER_TOKENS = {"tbd", "todo", "placeholder", "fixme", "not_implemented", "fill_in", "<insert>"}
MISSING_ID_TOKENS = {"", "none", "null", "na", "n/a", "nan", "tbd", "todo", "placeholder"}
PRIMARY_ID_COLS = {
    "id",
    "row_id",
    "device_id",
    "register",
    "register_id",
    "modbus_register",
    "bacnet_object",
    "tag",
    "tag_id",
    "element_id",
    "circuit_id",
    "panel_id",
    "breaker_id",
    "equipment_id",
    "point_id",
    "asset_id",
    "sensor_id",
    "meter_id",
}
STRICT_ID_COLS = {"id", "device_id", "register", "register_id", "tag_id", "element_id"}
ID_COL_CROSSCHECK = {"register", "register_id", "device_id"}
ZERO_BYTE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".json", ".ifc", ".pdf", ".docx", ".zip"}
UNIT_VAL_RE = re.compile(
    r"^[+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*"
    r"(mm|cm|m|km|in|ft|yd|kw|kva|mw|w|v|kv|a|ma|lb|lbs|kg|g|psi|kpa|bar|gpm|cfm|btu|sf|sqft|m2|deg|c|f|usd)$",
    re.I,
)
PLAIN_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:,\d{3})*|\d*\.\d+)(?:e[+-]?\d+)?$", re.I)
UNIT_HEADER_RE = re.compile(
    r"(?:_|-)(mm|cm|m|km|in|ft|kw|kva|mw|w|v|kv|a|ma|lb|lbs|kg|psi|kpa|bar|gpm|cfm|btu|sf|sqft|m2|usd)$",
    re.I,
)
NON_SOLID_CLASSES = {
    "IfcSpace",
    "IfcOpeningElement",
    "IfcSite",
    "IfcAnnotation",
    "IfcGrid",
    "IfcVirtualElement",
}
MEP_CLASSES = {
    "IfcDistributionElement",
    "IfcDistributionFlowElement",
    "IfcFlowSegment",
    "IfcFlowFitting",
    "IfcFlowController",
    "IfcFlowTerminal",
    "IfcFlowMovingDevice",
    "IfcFlowStorageDevice",
    "IfcFlowTreatmentDevice",
    "IfcEnergyConversionDevice",
    "IfcCableCarrierSegment",
    "IfcCableCarrierFitting",
    "IfcCableSegment",
    "IfcCableFitting",
    "IfcPipeSegment",
    "IfcPipeFitting",
    "IfcDuctSegment",
    "IfcDuctFitting",
    "IfcDistributionControlElement",
}
ROAD_NAME_RE = re.compile(r"(?:^|[^a-z0-9])(road|roadway|driveway|access_road|street|pavement)", re.I)
TRAY_RE = re.compile(r"tray", re.I)
TRAY_TAG_RE = re.compile(r"(?:^|[^a-z0-9])ct[-_]?\d+(?:[^a-z0-9]|$)", re.I)
BUSWAY_RE = re.compile(r"(?:^|[^a-z0-9])(busway|bus_?duct|bus-duct|bus|bw[-_]?\d+)(?:[^a-z0-9]|$)", re.I)
CHW_PIPE_RE = re.compile(r"(?:pipe|(?:^|[^a-z0-9])chw[-_]?\d*(?:[^a-z0-9]|$))", re.I)
MEP_NAME_RE = re.compile(r"(tray|busway|bus_?duct|conduit|pipe|duct|chilled|cable|(?:^|[^a-z0-9])(?:ct|bw|chw)[-_]?\d+)", re.I)
SPRINKLER_RE = re.compile(r"(sprinkler|(?:^|[^a-z0-9])spr[-_])", re.I)
EPS_MM = 1e-3
EPS_M = EPS_MM / 1000.0
GRID_CELL_M = 0.300
CONNECTIVITY_TOL_M = 0.250 + EPS_M
SEP_LIMIT_M = 0.300 - EPS_M
STACK_LIMIT_M = 0.300 - EPS_M
SUMMARY_ROW_RE = re.compile(
    r"^(total|subtotal|grand\s+total|summary|sum|average|avg|mean|min|max|note|notes)\b",
    re.I,
)
TOTAL_ROW_RE = re.compile(r"^(?:grand\s+total|total|sum)\b", re.I)
NON_SUM_COL_RE = re.compile(
    r"(?:^|_)(id|no|num|number|code|register|tag|row|index|avg|average|mean|min|max|"
    r"rate|unit_price|unit_cost|price|pct|percent|ratio|factor|pf|eff|efficiency|"
    r"voltage|volt|v|temp|temperature|deg|hz|freq|year|date|level|floor|tier|rev|version)(?:_|$)",
    re.I,
)

errors = []
warnings = []
pre_verifier_blockers = []
missing_deliverables = []
delivered_paths = set()
register_checked = False
combined_spec_texts = []
root = Path.cwd()
files = []
for cur, dirs, names in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
    for name in sorted(names):
        if name.startswith("."):
            continue
        p = Path(cur) / name
        if p.is_file():
            files.append(p)
        if len(files) >= 600:
            break
    if len(files) >= 600:
        break

def _rel(p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except Exception:
        return str(p)

def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")

def _reject_const(token: str):
    raise ValueError(f"non-finite JSON constant {token}")

id_registry = {}
table_id_sets = []

def _has_formula_or_nan(tok: str) -> bool:
    s = tok.strip()
    if not s:
        return False
    up = s.upper()
    return s.lower() in NAN_TOKENS or any(fe in up for fe in FORMULA_ERRORS)

def _check_headers_and_id_rows(rel: str, header, id_rows):
    norm_headers = [_norm_col(h) for h in header]
    seen_hdrs = {}
    for c_idx, nh in enumerate(norm_headers):
        if not nh:
            continue
        if nh in seen_hdrs:
            errors.append(
                f"{rel}: duplicate column header {header[c_idx]!r} (matches col {seen_hdrs[nh] + 1})"
            )
        else:
            seen_hdrs[nh] = c_idx
    if not id_rows:
        return
    for c_idx, nh in enumerate(norm_headers):
        col_vals = [
            str(r[c_idx]).strip() if c_idx < len(r) and r[c_idx] is not None else ""
            for r in id_rows
        ]
        if nh in PRIMARY_ID_COLS:
            missing = []
            exact_vals = set()
            for r_off, raw_cell in enumerate(id_rows):
                cell_str = str(raw_cell[c_idx]) if c_idx < len(raw_cell) and raw_cell[c_idx] is not None else ""
                if "\n" in cell_str or "\r" in cell_str or any(ord(ch) < 32 for ch in cell_str):
                    errors.append(
                        f"{rel}: control character/newline inside ID column {header[c_idx]!r} row {r_off + 2}"
                    )
                v = cell_str.strip()
                if v.lower() in MISSING_ID_TOKENS:
                    missing.append(r_off + 2)
                else:
                    exact_vals.add(v)
                    reg_map = id_registry.setdefault(nh, {})
                    k_low = v.lower()
                    if k_low in reg_map:
                        prev_val, prev_rel = reg_map[k_low]
                        if prev_val != v:
                            errors.append(
                                f"{rel}: register/ID case/whitespace mismatch on {header[c_idx]!r}: "
                                f"{v!r} vs {prev_val!r} ({prev_rel})"
                            )
                    else:
                        reg_map[k_low] = (v, rel)
            has_placeholder = any(col_vals[i - 2].lower() in PLACEHOLDER_TOKENS for i in missing)
            if missing and (len(missing) < len(col_vals) or has_placeholder or nh in STRICT_ID_COLS):
                errors.append(
                    f"{rel}: column {header[c_idx]!r} has missing/placeholder IDs on {len(missing)}/{len(col_vals)} "
                    f"non-summary row(s) (first at row {missing[0]})"
                )
            if nh in ID_COL_CROSSCHECK and exact_vals:
                table_id_sets.append((rel, nh, exact_vals))
        else:
            nonempty = [v for v in col_vals if v and v.lower() not in MISSING_ID_TOKENS]
            if not nonempty:
                continue
            unit_hits = [v for v in nonempty if UNIT_VAL_RE.match(v)]
            if not unit_hits:
                continue
            plain_cnt = sum(1 for v in nonempty if PLAIN_NUM_RE.match(v))
            if UNIT_HEADER_RE.search(nh) or (len(nonempty) >= 3 and plain_cnt / len(nonempty) >= 0.6):
                errors.append(
                    f"{rel}: unparsed unit string {unit_hits[0]!r} in numeric/unit column {header[c_idx]!r}"
                )

def _parse_summary_num(raw):
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) else None
    s = str(raw).strip()
    if not s or s.startswith("=") or _has_formula_or_nan(s):
        return None
    s = s.replace(",", "")
    if s.startswith("-$"):
        s = "-" + s[2:].strip()
    elif s.startswith("+$"):
        s = s[2:].strip()
    elif s.startswith("$"):
        s = s[1:].strip()
    try:
        val = float(s)
    except ValueError:
        return None
    return val if math.isfinite(val) else None

def _check_summary_totals(rel: str, header, indexed_rows):
    if not header or not indexed_rows:
        return
    all_detail_rows = []
    section_rows = []
    total_entries = []
    for r_idx, row_vals in indexed_rows:
        first_ne = next(
            (str(c).strip() for c in row_vals if c is not None and str(c).strip()),
            "",
        )
        if first_ne and SUMMARY_ROW_RE.match(first_ne):
            if TOTAL_ROW_RE.match(first_ne):
                is_grand = bool(re.match(r"^grand\s+total\b", first_ne, re.I))
                total_entries.append((r_idx, is_grand, row_vals, list(section_rows)))
            section_rows = []
        else:
            all_detail_rows.append(row_vals)
            section_rows.append(row_vals)
    if not total_entries or len(all_detail_rows) < 2:
        return
    for r_idx, is_grand, trow, sec_rows in total_entries:
        if is_grand or len(total_entries) == 1:
            cand_sets = [all_detail_rows]
        else:
            cand_sets = [s for s in (sec_rows, all_detail_rows) if len(s) >= 2]
        if not cand_sets:
            continue
        for c_idx, h_raw in enumerate(header):
            nh = _norm_col(h_raw)
            if not nh or nh in PRIMARY_ID_COLS or NON_SUM_COL_RE.search(nh):
                continue
            reported = _parse_summary_num(trow[c_idx]) if c_idx < len(trow) else None
            if reported is None:
                continue
            qualifying = []
            for cset in cand_sets:
                nonempty = [
                    r[c_idx]
                    for r in cset
                    if c_idx < len(r)
                    and r[c_idx] is not None
                    and str(r[c_idx]).strip()
                    and str(r[c_idx]).strip().lower() not in {"-", "--", "n/a", "na"}
                ]
                if not nonempty:
                    continue
                nums = [v for v in (_parse_summary_num(cell) for cell in nonempty) if v is not None]
                if len(nums) >= 2 and (len(nums) / len(nonempty)) >= 0.8:
                    qualifying.append((nums, sum(nums)))
            if not qualifying:
                continue
            if not any(
                abs(reported - exp) <= max(1e-2, 1e-3 * abs(exp))
                for _, exp in qualifying
            ):
                nums0, exp0 = qualifying[0]
                errors.append(
                    f"{rel}: summary total mismatch in column {h_raw!r} (row {r_idx}): "
                    f"reported {reported:g} vs sum of {len(nums0)} detail rows {exp0:g}"
                )

REQ_CELL_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:R-\d{2,6}-\d{1,4}|R-[A-Z]{2,12}(?:-\d{1,4})?|[A-Z]{1,6}-\d{1,6}|[RHS]\d{2,3})(?![A-Za-z0-9_])"
)

def _is_constraint_register(rel: str) -> bool:
    leaf = rel.rsplit("!", 1)[-1]
    stem = Path(leaf).stem.lower()
    norm = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    return "constraint_register" in norm or "constraints_register" in norm

def _is_register_sheet(sname: str) -> bool:
    low = str(sname or "").strip().lower()
    return "register" in low or "constraint" in low

def _req_id_family(rid: str) -> str:
    up = str(rid).strip().upper()
    if re.match(r"^R-\d{2,6}-\d{1,4}$", up) or re.match(r"^R-[A-Z]{2,12}(?:-\d{1,4})?$", up):
        return "R-hyphen:" + up.rsplit("-", 1)[0] + "-"
    if re.match(r"^R\d{2,3}$", up):
        return "R-num"
    if re.match(r"^H\d{2,3}$", up):
        return "H-num"
    if re.match(r"^S\d{2,3}$", up):
        return "S-num"
    if "-" in up:
        return "custom:" + up.rsplit("-", 1)[0] + "-"
    return "custom:" + "".join(ch for ch in up if ch.isalpha())

def _check_constraint_register_coverage(rel: str, cell_values):
    global register_checked
    if not REQUIRED_REGISTER_IDS:
        return
    if not _is_constraint_register(rel) and "[" not in rel:
        return
    register_checked = True
    found_tokens = set()
    for val in cell_values:
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        if " " not in s and len(s) <= 40:
            found_tokens.add(s.upper())
        for tok in REQ_CELL_ID_RE.findall(s):
            found_tokens.add(tok.upper())
    present_families = {_req_id_family(tok) for tok in found_tokens if any(ch.isdigit() for ch in tok) or tok.startswith("R-")}
    required_families = {_req_id_family(rid) for rid in REQUIRED_REGISTER_IDS}
    matched_families = present_families & required_families
    has_r_family = any(f.startswith("R-") for f in present_families)
    if matched_families or has_r_family:
        targets = [
            rid
            for rid in REQUIRED_REGISTER_IDS
            if _req_id_family(rid) in matched_families
            or (has_r_family and _req_id_family(rid).startswith("R-"))
        ]
    else:
        targets = list(REQUIRED_REGISTER_IDS)
    missing = [rid for rid in targets if rid.upper() not in found_tokens]
    if missing:
        msg = f"{rel}: constraint_register missing {len(missing)} required ID(s): {missing[:15]}"
        errors.append(msg)

def _audit_csv_text(rel: str, text: str):
    if not text.strip():
        errors.append(f"{rel}: deliverable file is empty (0 bytes/blank)")
        return
    if text.lstrip().startswith("```"):
        errors.append(f"{rel}: CSV starts with markdown code fence")
        return
    try:
        rows = list(csv.reader(text.splitlines()))
    except Exception as exc:
        errors.append(f"{rel}: CSV parse error: {exc}")
        return
    if not rows:
        errors.append(f"{rel}: empty CSV file")
        return
    header = rows[0]
    if not header or all(not str(c).strip() for c in header):
        errors.append(f"{rel}: blank CSV header row")
        return
    if REQUIRED_REGISTER_IDS and _is_constraint_register(rel):
        _check_constraint_register_coverage(rel, [c for r in rows[1:] for c in r])
    data_rows = [r for r in rows[1:] if any(str(c).strip() for c in r)]
    bad_cols = [idx + 2 for idx, r in enumerate(rows[1:]) if r and len(r) != len(header)]
    if bad_cols:
        errors.append(
            f"{rel}: shifted/malformed CSV columns on {len(bad_cols)} row(s) "
            f"(first at row {bad_cols[0]}: expected {len(header)} cols)"
        )
    if not data_rows:
        warnings.append(f"{rel}: CSV has header only (0 data rows)")
        return
    valid_indexed = [
        (idx + 2, r)
        for idx, r in enumerate(rows[1:])
        if any(str(c).strip() for c in r) and len(r) == len(header)
    ]
    valid_rows = [r for _, r in valid_indexed]
    seen_rows = set()
    dup_row_count = 0
    for r_idx, r in valid_indexed:
        row_key = tuple(c.strip() for c in r)
        if row_key in seen_rows:
            dup_row_count += 1
        else:
            seen_rows.add(row_key)
        for c_idx, cell in enumerate(r):
            if _has_formula_or_nan(cell):
                errors.append(
                    f"{rel}: invalid numeric/formula token {cell.strip()!r} at row {r_idx}, col {header[c_idx]!r}"
                )
                break
    if dup_row_count:
        warnings.append(f"{rel}: {dup_row_count} exact duplicate data row(s)")
    id_rows = []
    for r in valid_rows:
        first_nonempty = next((c.strip() for c in r if c.strip()), "")
        if first_nonempty and SUMMARY_ROW_RE.match(first_nonempty):
            continue
        id_rows.append(r)
    _check_headers_and_id_rows(rel, header, id_rows)
    _check_summary_totals(rel, header, valid_indexed)

def _xml_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}", 1)[-1] if "}" in tag else tag

def _col_letters_to_idx(ref: str):
    letters = "".join(ch for ch in str(ref or "") if ch.isalpha()).upper()
    if not letters:
        return None
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1

def _audit_xlsx_stdlib(rel: str, raw_bytes: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            znames = set(zf.namelist())
            zlower = {n.lower(): n for n in znames}
            shared_strings = []
            ss_name = zlower.get("xl/sharedstrings.xml")
            if ss_name:
                ss_root = ET.fromstring(zf.read(ss_name))
                for child in ss_root:
                    if _xml_tag(child) == "si":
                        txt = "".join(
                            t.text or ""
                            for t in child.iter()
                            if _xml_tag(t) == "t"
                        )
                        shared_strings.append(txt)
            rel_map = {}
            rels_name = zlower.get("xl/_rels/workbook.xml.rels")
            if rels_name:
                rels_root = ET.fromstring(zf.read(rels_name))
                for rel_el in rels_root:
                    if _xml_tag(rel_el) == "Relationship":
                        rid = rel_el.attrib.get("Id", "")
                        tgt = rel_el.attrib.get("Target", "")
                        if rid and tgt:
                            clean_t = tgt.lstrip("/")
                            if not clean_t.lower().startswith("xl/"):
                                clean_t = "xl/" + clean_t.lstrip("./")
                            rel_map[rid] = clean_t
            sheets = []
            wb_name = zlower.get("xl/workbook.xml")
            if wb_name:
                wb_root = ET.fromstring(zf.read(wb_name))
                for sh_el in wb_root.iter():
                    if _xml_tag(sh_el) == "sheet":
                        sname = sh_el.attrib.get("name") or f"Sheet{len(sheets) + 1}"
                        rid = ""
                        for ak, av in sh_el.attrib.items():
                            if ak == "r:id" or ak.endswith("}id"):
                                rid = av
                                break
                        tgt = rel_map.get(rid, "")
                        actual = zlower.get(tgt.lower()) if tgt else None
                        if actual:
                            sheets.append((sname, actual))
            if not sheets:
                ws_names = sorted(
                    n for n in znames if re.match(r"^xl/worksheets/sheet\d*\.xml$", n, re.I)
                )
                sheets = [(Path(n).stem, n) for n in ws_names]
            if not sheets:
                return
            check_reg_wb = bool(REQUIRED_REGISTER_IDS and _is_constraint_register(rel))
            register_cells = []
            for sname, ws_path in sheets:
                ws_root = ET.fromstring(zf.read(ws_path))
                sheet_header = None
                bad_cols = []
                id_rows = []
                all_aligned = []
                sheet_reg_cells = []
                check_reg_sheet = bool(REQUIRED_REGISTER_IDS and _is_register_sheet(sname))
                auto_r = 0
                for row_el in ws_root.iter():
                    if _xml_tag(row_el) != "row":
                        continue
                    auto_r += 1
                    if auto_r > 20000:
                        break
                    r_attr = row_el.attrib.get("r")
                    try:
                        r_idx = int(r_attr) if r_attr else auto_r
                    except ValueError:
                        r_idx = auto_r
                    row_cells = {}
                    seq_c = 0
                    for c_el in row_el:
                        if _xml_tag(c_el) != "c":
                            continue
                        parsed_c = _col_letters_to_idx(c_el.attrib.get("r", ""))
                        c_idx = parsed_c if parsed_c is not None else seq_c
                        seq_c = c_idx + 1
                        ctype = c_el.attrib.get("t", "")
                        f_text = None
                        v_text = None
                        is_text = None
                        for child in c_el:
                            ctag = _xml_tag(child)
                            if ctag == "f" and child.text:
                                f_text = child.text
                            elif ctag == "v" and child.text is not None:
                                v_text = child.text
                            elif ctag == "is":
                                is_text = "".join(
                                    t.text or ""
                                    for t in child.iter()
                                    if _xml_tag(t) == "t"
                                )
                        if f_text and _has_formula_or_nan(f_text):
                            errors.append(
                                f"{rel}[{sname}] R{r_idx}C{c_idx + 1}: Excel error/NaN token {f_text.strip()!r}"
                            )
                        val = None
                        if ctype == "s":
                            if v_text is not None:
                                try:
                                    si_idx = int(v_text.strip())
                                    if 0 <= si_idx < len(shared_strings):
                                        val = shared_strings[si_idx]
                                except ValueError:
                                    val = v_text
                        elif ctype == "inlineStr":
                            val = is_text if is_text is not None else v_text
                        elif ctype == "b":
                            val = "TRUE" if str(v_text or "").strip() == "1" else "FALSE"
                        else:
                            val = v_text
                        if val is not None:
                            sval = str(val)
                            if _has_formula_or_nan(sval):
                                errors.append(
                                    f"{rel}[{sname}] R{r_idx}C{c_idx + 1}: Excel error/NaN token {sval.strip()!r}"
                                )
                            elif ctype in ("", "n"):
                                try:
                                    fval = float(sval.strip())
                                    if not math.isfinite(fval):
                                        errors.append(
                                            f"{rel}[{sname}] R{r_idx}C{c_idx + 1}: non-finite float {fval}"
                                        )
                                except ValueError:
                                    pass
                        row_cells[c_idx] = val
                    nonempty_cols = [
                        ci for ci, cv in row_cells.items() if cv is not None and str(cv).strip()
                    ]
                    if not nonempty_cols:
                        continue
                    max_c = max(nonempty_cols)
                    if sheet_header is None:
                        sheet_header = [
                            str(row_cells.get(i, "") or "") for i in range(max_c + 1)
                        ]
                        continue
                    if check_reg_wb:
                        register_cells.extend(cv for cv in row_cells.values() if cv is not None)
                    if check_reg_sheet:
                        sheet_reg_cells.extend(cv for cv in row_cells.values() if cv is not None)
                    if max_c >= len(sheet_header):
                        bad_cols.append(r_idx)
                        continue
                    aligned = [row_cells.get(i, "") for i in range(len(sheet_header))]
                    all_aligned.append((r_idx, aligned))
                    first_ne = next(
                        (str(c).strip() for c in aligned if c is not None and str(c).strip()),
                        "",
                    )
                    if not (first_ne and SUMMARY_ROW_RE.match(first_ne)):
                        id_rows.append(aligned)
                if bad_cols and sheet_header is not None:
                    errors.append(
                        f"{rel}[{sname}]: shifted/malformed XLSX columns on {len(bad_cols)} row(s) "
                        f"(first at row {bad_cols[0]}: expected {len(sheet_header)} cols)"
                    )
                if sheet_header and any(h.strip() for h in sheet_header):
                    if id_rows:
                        _check_headers_and_id_rows(f"{rel}[{sname}]", sheet_header, id_rows)
                    _check_summary_totals(f"{rel}[{sname}]", sheet_header, all_aligned)
                if check_reg_sheet and not check_reg_wb:
                    _check_constraint_register_coverage(f"{rel}[{sname}]", sheet_reg_cells)
            if check_reg_wb:
                _check_constraint_register_coverage(rel, register_cells)
    except Exception as exc:
        errors.append(f"{rel}: failed reading XLSX XML (stdlib): {exc}")

def _audit_xlsx_bytes(rel: str, raw_bytes: bytes):
    if not raw_bytes:
        errors.append(f"{rel}: deliverable file is empty (0 bytes)")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            znames = set(zf.namelist())
            if "[Content_Types].xml" not in znames or not any(
                n.lower() == "xl/workbook.xml" or n.lower().endswith("/workbook.xml")
                for n in znames
            ):
                errors.append(f"{rel}: invalid XLSX container (missing [Content_Types].xml or xl/workbook.xml)")
                return
    except Exception as exc:
        errors.append(f"{rel}: corrupt XLSX/ZIP container: {exc}")
        return
    try:
        import openpyxl
    except ImportError:
        openpyxl = None
    if openpyxl is None:
        _audit_xlsx_stdlib(rel, raw_bytes)
        return
    if openpyxl is not None:
        sheet_headers = {}
        check_reg_wb = bool(REQUIRED_REGISTER_IDS and _is_constraint_register(rel))
        register_cells = []
        sheet_reg_cells = {}
        saw_formula_pass = False
        for data_only in (False, True):
            wb = None
            try:
                wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=data_only)
                for sname in wb.sheetnames:
                    ws = wb[sname]
                    sheet_header = sheet_headers.get(sname)
                    sheet_rows = []
                    calc_indexed = []
                    bad_cols = []
                    check_reg_sheet = bool(REQUIRED_REGISTER_IDS and _is_register_sheet(sname))
                    if check_reg_sheet and not data_only:
                        sheet_reg_cells.setdefault(sname, [])
                    for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                        if r_idx > 20000:
                            break
                        last_ne = max(
                            (i for i, c in enumerate(row) if c is not None and str(c).strip()),
                            default=-1,
                        )
                        if not data_only and r_idx == 1 and last_ne >= 0:
                            sheet_header = [str(row[i]) if row[i] is not None else "" for i in range(last_ne + 1)]
                            sheet_headers[sname] = sheet_header
                        elif r_idx > 1 and last_ne >= 0:
                            if check_reg_wb and not data_only:
                                register_cells.extend(c for c in row if c is not None)
                            if check_reg_sheet and not data_only:
                                sheet_reg_cells[sname].extend(c for c in row if c is not None)
                            if sheet_header is not None and last_ne >= len(sheet_header):
                                if not data_only:
                                    bad_cols.append(r_idx)
                            else:
                                aligned = list(row[: len(sheet_header)]) if sheet_header else list(row)
                                if not data_only:
                                    first_ne = next((str(c).strip() for c in aligned if c is not None and str(c).strip()), "")
                                    if not (first_ne and SUMMARY_ROW_RE.match(first_ne)):
                                        sheet_rows.append(aligned)
                                elif sheet_header is not None:
                                    calc_indexed.append((r_idx, aligned))
                        for c_idx, val in enumerate(row, start=1):
                            if val is None:
                                continue
                            if isinstance(val, float) and not math.isfinite(val):
                                errors.append(f"{rel}[{sname}] R{r_idx}C{c_idx}: non-finite float {val}")
                            elif isinstance(val, str) and _has_formula_or_nan(val):
                                errors.append(f"{rel}[{sname}] R{r_idx}C{c_idx}: Excel error/NaN token {val.strip()!r}")
                    if not data_only and bad_cols and sheet_header is not None:
                        errors.append(
                            f"{rel}[{sname}]: shifted/malformed XLSX columns on {len(bad_cols)} row(s) "
                            f"(first at row {bad_cols[0]}: expected {len(sheet_header)} cols)"
                        )
                    if not data_only and sheet_header and any(h.strip() for h in sheet_header) and sheet_rows:
                        _check_headers_and_id_rows(f"{rel}[{sname}]", sheet_header, sheet_rows)
                    if data_only and sheet_header and any(h.strip() for h in sheet_header) and calc_indexed:
                        _check_summary_totals(f"{rel}[{sname}]", sheet_header, calc_indexed)
                if not data_only:
                    saw_formula_pass = True
            except Exception as exc:
                errors.append(f"{rel}: failed reading workbook (data_only={data_only}): {exc}")
            finally:
                if wb is not None:
                    try:
                        wb.close()
                    except Exception:
                        pass
        if saw_formula_pass:
            if check_reg_wb:
                _check_constraint_register_coverage(rel, register_cells)
            else:
                for sname, s_cells in sheet_reg_cells.items():
                    _check_constraint_register_coverage(f"{rel}[{sname}]", s_cells)

def _audit_json_text(rel: str, text: str):
    if not text.strip():
        errors.append(f"{rel}: empty JSON deliverable")
        return
    try:
        parsed = json.loads(text, parse_constant=_reject_const)
        stack = [parsed]
        seen_nodes = 0
        while stack and seen_nodes < 10000:
            cur_val = stack.pop()
            seen_nodes += 1
            if isinstance(cur_val, dict):
                stack.extend(cur_val.values())
            elif isinstance(cur_val, list):
                stack.extend(cur_val)
            elif isinstance(cur_val, float) and not math.isfinite(cur_val):
                errors.append(f"{rel}: non-finite float in JSON")
                break
            elif isinstance(cur_val, str) and any(fe in cur_val.strip().upper() for fe in FORMULA_ERRORS):
                errors.append(f"{rel}: spreadsheet error string {cur_val!r} in JSON")
                break
    except Exception as exc:
        errors.append(f"{rel}: invalid JSON or non-finite numeric literal: {exc}")

_DOCX_BLANK_RE = re.compile(r"\[\s*_+\s*\]")
_CONCAT_AS_IND_RE = re.compile(r"[A-Za-z0-9]+as indicated\b")
_BARE_AS_IND_RE = re.compile(r"(?m)^\s*(?:\d+\.|[a-z]\.)\s*as indicated\.?\s*$")
_ADJ_BRACKET_RE = re.compile(r"\]\s*\[")
_XREF_COMMA_RE = re.compile(r"\bSection\s+\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?,\s+[A-Z]")
_SECTION_XREF_RE = re.compile(r"\bSection\s+(\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?)\b")
_UFC_CITE_RE = re.compile(r"\bUFC\s+\d+-\d+-\d+\b|\bUnified\s+Facilities\s+Criteria\b", re.I)
_SKIP_DOC_MD_NAMES = {"task.md", "readme.md", "plan.md", "notes.md"}

def _record_pre_verifier_issue(msg):
    if msg not in errors:
        errors.append(msg)
    if msg not in pre_verifier_blockers:
        pre_verifier_blockers.append(msg)

def _audit_spec_text(rel: str, text: str, is_docx: bool = False):
    if not text or not text.strip():
        return
    combined_spec_texts.append(text)
    if _DOCX_BLANK_RE.search(text):
        _record_pre_verifier_issue(f"{rel}: unresolved bracket blank '[_____]' placeholder in specification text")
    m_cat = _CONCAT_AS_IND_RE.search(text)
    if m_cat:
        _record_pre_verifier_issue(f"{rel}: concatenated placeholder artifact {m_cat.group(0)!r}")
    if _BARE_AS_IND_RE.search(text):
        _record_pre_verifier_issue(f"{rel}: bare standalone 'as indicated' line replacing blank placeholder")
    if is_docx and _ADJ_BRACKET_RE.search(text):
        _record_pre_verifier_issue(f"{rel}: adjacent unresolved bracketed choices ']['")
    m_cm = _XREF_COMMA_RE.search(text)
    if m_cm:
        _record_pre_verifier_issue(
            f"{rel}: disallowed comma between section number and title in cross-reference {m_cm.group(0)!r}"
        )
    for raw_tok in FORBIDDEN_CITATIONS:
        tok = str(raw_tok).strip()
        if not tok:
            continue
        if tok.upper() == "UFC":
            m_u = _UFC_CITE_RE.search(text)
            if m_u:
                _record_pre_verifier_issue(f"{rel}: forbidden citation per task exclusion rules: {m_u.group(0)!r}")
        else:
            m_f = re.search(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", text, re.I)
            if m_f:
                _record_pre_verifier_issue(f"{rel}: forbidden citation per task exclusion rules: {m_f.group(0)!r}")
    seen_secs = set()
    allowed_set = set(ALLOWED_SECTIONS)
    disallowed_set = set(DISALLOWED_SECTIONS)
    for m_s in _SECTION_XREF_RE.finditer(text):
        sec_num = " ".join(m_s.group(1).split())
        if sec_num in seen_secs:
            continue
        if sec_num in disallowed_set:
            seen_secs.add(sec_num)
            _record_pre_verifier_issue(f"{rel}: disallowed section cross-reference 'Section {sec_num}'")
        elif allowed_set and sec_num not in allowed_set:
            seen_secs.add(sec_num)
            _record_pre_verifier_issue(
                f"{rel}: section cross-reference 'Section {sec_num}' not in allowed project manual sections"
            )

def _audit_docx_bytes(rel: str, raw_bytes: bytes):
    if not raw_bytes:
        errors.append(f"{rel}: deliverable file is empty (0 bytes)")
        return
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as dz:
            if "word/document.xml" not in set(dz.namelist()):
                errors.append(f"{rel}: invalid DOCX archive (missing word/document.xml)")
                return
            doc_root = ET.fromstring(dz.read("word/document.xml"))
        paras = []
        for el in doc_root.iter():
            if _xml_tag(el) == "p":
                ptxt = "".join(t.text or "" for t in el.iter() if _xml_tag(t) == "t")
                if ptxt.strip():
                    paras.append(ptxt)
        _audit_spec_text(rel, "\n".join(paras), is_docx=True)
    except Exception as exc:
        errors.append(f"{rel}: corrupt DOCX container: {exc}")

def _audit_zip_file(rel: str, p: Path):
    total_extracted = 0
    try:
        with zipfile.ZipFile(p, "r") as zf:
            for info in sorted(zf.infolist(), key=lambda x: x.filename):
                if info.is_dir():
                    continue
                parts = Path(info.filename).parts
                if any(part.startswith(".") or part == "__MACOSX" for part in parts):
                    continue
                if info.file_size > 10 * 1024 * 1024:
                    continue
                if total_extracted + info.file_size > 25 * 1024 * 1024:
                    break
                total_extracted += info.file_size
                norm_member = info.filename.replace("\\", "/").lstrip("./")
                delivered_paths.add(norm_member.lower())
                mrel = f"{rel}!{info.filename}"
                delivered_paths.add(mrel.lower())
                mext = Path(info.filename).suffix.lower()
                raw_bytes = zf.read(info.filename)
                if mext in ZERO_BYTE_EXTS and not raw_bytes:
                    errors.append(f"{mrel}: deliverable file is empty (0 bytes)")
                    continue
                if mext == ".csv":
                    _audit_csv_text(mrel, raw_bytes.decode("utf-8-sig", errors="replace"))
                elif mext in {".xlsx", ".xlsm"}:
                    _audit_xlsx_bytes(mrel, raw_bytes)
                elif mext == ".docx":
                    _audit_docx_bytes(mrel, raw_bytes)
                elif mext == ".md" and Path(info.filename).name.lower() not in _SKIP_DOC_MD_NAMES:
                    _audit_spec_text(mrel, raw_bytes.decode("utf-8", errors="replace"), is_docx=False)
                elif mext == ".json" and Path(info.filename).name.lower() not in SKIP_JSON_NAMES:
                    _audit_json_text(mrel, raw_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        errors.append(f"{rel}: corrupt ZIP archive: {exc}")

AUDITABLE_EXTS = {".xlsx", ".xlsm", ".csv", ".tsv", ".ifc", ".json", ".zip", ".docx", ".md"}
LARGE_AUDITABLE_EXTS = {".ifc", ".zip"}

def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def _is_target_deliverable(rel: str) -> bool:
    if not TARGET_DELIVERABLES:
        return False
    rel_low = rel.replace("\\", "/").lstrip("./").lower()
    for t in TARGET_DELIVERABLES:
        t_low = str(t).replace("\\", "/").lstrip("./").lower()
        if not t_low:
            continue
        if "/" in t_low:
            if rel_low == t_low:
                return True
        elif rel_low == t_low or rel_low == f"output/{t_low}":
            return True
    return False

ifc_files = []
skipped_unmodified_xlsx = []
skipped_unmodified_inputs = []
mep_summary = {}
clear_height = {"routed_mep": [], "terminals": []}
for p in files:
    rel = _rel(p)
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        continue
    if ext in AUDITABLE_EXTS and BASELINE_FILES and not _is_target_deliverable(rel):
        base_entry = BASELINE_FILES.get(rel)
        if isinstance(base_entry, (list, tuple)) and len(base_entry) >= 2:
            max_base_bytes = 50 * 1024 * 1024 if ext in LARGE_AUDITABLE_EXTS else 25 * 1024 * 1024
            try:
                if (
                    size == int(base_entry[0])
                    and size <= max_base_bytes
                    and _file_sha256(p) == str(base_entry[1])
                ):
                    skipped_unmodified_inputs.append(rel)
                    if ext in {".xlsx", ".xlsm"}:
                        skipped_unmodified_xlsx.append(rel)
                    continue
            except (TypeError, ValueError, OSError):
                pass
    if ext in ZERO_BYTE_EXTS and size == 0:
        errors.append(f"{rel}: deliverable file is empty (0 bytes)")
        continue
    if ext == ".ifc":
        ifc_files.append(p)
        continue
    if ext == ".zip":
        if size <= 50 * 1024 * 1024:
            _audit_zip_file(rel, p)
        continue
    if size > 25 * 1024 * 1024:
        continue
    if ext == ".csv":
        delivered_paths.add(rel.lower())
        _audit_csv_text(rel, p.read_text(encoding="utf-8-sig", errors="replace"))
    elif ext in {".xlsx", ".xlsm"}:
        delivered_paths.add(rel.lower())
        _audit_xlsx_bytes(rel, p.read_bytes())
    elif ext == ".docx":
        delivered_paths.add(rel.lower())
        _audit_docx_bytes(rel, p.read_bytes())
    elif ext == ".md" and p.name.lower() not in _SKIP_DOC_MD_NAMES:
        delivered_paths.add(rel.lower())
        _audit_spec_text(rel, p.read_text(encoding="utf-8", errors="replace"), is_docx=False)
    elif ext == ".json" and p.name.lower() not in SKIP_JSON_NAMES:
        delivered_paths.add(rel.lower())
        _audit_json_text(rel, p.read_text(encoding="utf-8", errors="replace"))
    elif ext in {".zip", ".ifc"}:
        delivered_paths.add(rel.lower())

if REQUIRED_SCOPE_EXCLUSIONS and combined_spec_texts:
    _full_spec_low = "\n".join(combined_spec_texts).lower()
    for _excl_phrase in REQUIRED_SCOPE_EXCLUSIONS:
        _ep = str(_excl_phrase).strip()
        if _ep and _ep.lower() not in _full_spec_low:
            _record_pre_verifier_issue(f"deliverable specs: required scope exclusion missing: {_ep!r}")

if TARGET_DELIVERABLES:
    _all_workspace_rels = {_rel(fp).replace("\\", "/").lstrip("./").lower() for fp in files} | delivered_paths
    for _td in TARGET_DELIVERABLES:
        _td_low = str(_td).replace("\\", "/").lstrip("./").lower()
        if not _td_low:
            continue
        _found_td = (
            (_td_low in _all_workspace_rels)
            or (f"output/{_td_low}" in _all_workspace_rels)
            or any(dp.endswith("/" + _td_low) or dp.endswith("!" + _td_low) for dp in _all_workspace_rels)
        )
        if not _found_td:
            _miss_msg = f"{_td}: missing required target deliverable on disk or in submission archive"
            missing_deliverables.append(_miss_msg)
            if ENFORCE_DELIVERABLES:
                errors.append(_miss_msg)

cross_warns = 0
for i in range(len(table_id_sets)):
    if cross_warns >= 5:
        break
    r1, c1, s1 = table_id_sets[i]
    for j in range(i + 1, len(table_id_sets)):
        r2, c2, s2 = table_id_sets[j]
        if c1 == c2 and s1 != s2:
            cross_warns += 1
            warnings.append(
                f"cross-table {c1!r} mismatch between {r1} ({len(s1)} IDs) and {r2} ({len(s2)} IDs): "
                f"diff sample {sorted(s1 ^ s2)[:5]}"
            )
            if cross_warns >= 5:
                break

# 2. Pure-Python 2D/3D geometry helpers for IFC site and MEP clash checks
def _aabb_of(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

def _aabb_overlap(a, b, tol=0.0):
    return (
        min(a[3], b[3]) - max(a[0], b[0]) > tol
        and min(a[4], b[4]) - max(a[1], b[1]) > tol
        and min(a[5], b[5]) - max(a[2], b[2]) > tol
    )

def _aabb_dist(a, b):
    dx = max(0.0, a[0] - b[3], b[0] - a[3])
    dy = max(0.0, a[1] - b[4], b[1] - a[4])
    dz = max(0.0, a[2] - b[5], b[2] - a[5])
    return math.sqrt(dx * dx + dy * dy + dz * dz)

def _poly_area2d(poly):
    if len(poly) < 3:
        return 0.0
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5

def _clip_poly2d(subject, clip):
    def _ensure_ccw(pts):
        s = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            s += (x2 - x1) * (y2 + y1)
        return list(reversed(pts)) if s > 0 else list(pts)

    def _inside(p, cx1, cy1, cx2, cy2):
        return (cx2 - cx1) * (p[1] - cy1) - (cy2 - cy1) * (p[0] - cx1) >= -1e-12

    def _inter(p1, p2, cx1, cy1, cx2, cy2):
        dcx, dcy = cx2 - cx1, cy2 - cy1
        dpx, dpy = p2[0] - p1[0], p2[1] - p1[1]
        den = dcx * dpy - dcy * dpx
        if abs(den) < 1e-15:
            return p2
        n1 = dcx * (p1[1] - cy1) - dcy * (p1[0] - cx1)
        t = -n1 / den
        return (p1[0] + t * dpx, p1[1] + t * dpy)

    clip_ccw = _ensure_ccw(clip)
    out = list(subject)
    for i in range(len(clip_ccw)):
        cx1, cy1 = clip_ccw[i]
        cx2, cy2 = clip_ccw[(i + 1) % len(clip_ccw)]
        if not out:
            break
        inp = out
        out = []
        s = inp[-1]
        for e in inp:
            if _inside(e, cx1, cy1, cx2, cy2):
                if not _inside(s, cx1, cy1, cx2, cy2):
                    out.append(_inter(s, e, cx1, cy1, cx2, cy2))
                out.append(e)
            elif _inside(s, cx1, cy1, cx2, cy2):
                out.append(_inter(s, e, cx1, cy1, cx2, cy2))
            s = e
    return out

def _footprint_overlap_area(tris_a, tris_b, shap):
    if shap is not None:
        Poly, u_union = shap
        pa = []
        for t in tris_a:
            p = Poly([(t[0][0], t[0][1]), (t[1][0], t[1][1]), (t[2][0], t[2][1])])
            if p.area > 1e-7:
                pa.append(p.buffer(0))
        pb = []
        for t in tris_b:
            p = Poly([(t[0][0], t[0][1]), (t[1][0], t[1][1]), (t[2][0], t[2][1])])
            if p.area > 1e-7:
                pb.append(p.buffer(0))
        if not pa or not pb:
            return 0.0
        return float(u_union(pa).intersection(u_union(pb)).area)
    total = 0.0
    for ta in tris_a:
        ta2 = [(p[0], p[1]) for p in ta]
        if _poly_area2d(ta2) <= 1e-7:
            continue
        ax0, ax1 = min(p[0] for p in ta2), max(p[0] for p in ta2)
        ay0, ay1 = min(p[1] for p in ta2), max(p[1] for p in ta2)
        for tb in tris_b:
            tb2 = [(p[0], p[1]) for p in tb]
            if _poly_area2d(tb2) <= 1e-7:
                continue
            if ax1 < min(p[0] for p in tb2) or max(p[0] for p in tb2) < ax0:
                continue
            if ay1 < min(p[1] for p in tb2) or max(p[1] for p in tb2) < ay0:
                continue
            clipped = _clip_poly2d(ta2, tb2)
            total += _poly_area2d(clipped)
    return total

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])

def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )

def _pt_tri_dist_sq(p, tri):
    a, b, c = tri
    ab = _sub(b, a)
    ac = _sub(c, a)
    ap = _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return _dot(ap, ap)
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return _dot(bp, bp)
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3)
        q = _add(a, _scale(ab, v))
        d = _sub(p, q)
        return _dot(d, d)
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return _dot(cp, cp)
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6)
        q = _add(a, _scale(ac, w))
        d = _sub(p, q)
        return _dot(d, d)
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        q = _add(b, _scale(_sub(c, b), w))
        d = _sub(p, q)
        return _dot(d, d)
    denom = 1.0 / (va + vb + vc) if (va + vb + vc) > 1e-18 else 0.0
    v, w = vb * denom, vc * denom
    q = _add(a, _add(_scale(ab, v), _scale(ac, w)))
    d = _sub(p, q)
    return _dot(d, d)

def _seg_seg_dist(p1, q1, p2, q2):
    d1 = _sub(q1, p1)
    d2 = _sub(q2, p2)
    r = _sub(p1, p2)
    a = _dot(d1, d1)
    e = _dot(d2, d2)
    f = _dot(d2, r)
    if a <= 1e-18 and e <= 1e-18:
        return math.sqrt(_dot(r, r))
    if a <= 1e-18:
        s = 0.0
        t = min(1.0, max(0.0, f / e))
    else:
        c = _dot(d1, r)
        if e <= 1e-18:
            t = 0.0
            s = min(1.0, max(0.0, -c / a))
        else:
            b = _dot(d1, d2)
            denom = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / denom)) if abs(denom) > 1e-18 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))
    c1 = _add(p1, _scale(d1, s))
    c2 = _add(p2, _scale(d2, t))
    diff = _sub(c1, c2)
    return math.sqrt(_dot(diff, diff))

def _ray_hits_tri(orig, direction, tri):
    a, b, c = tri
    e1 = _sub(b, a)
    e2 = _sub(c, a)
    h = _cross(direction, e2)
    det = _dot(e1, h)
    if abs(det) < 1e-12:
        return None
    inv = 1.0 / det
    s = _sub(orig, a)
    u = inv * _dot(s, h)
    if u < -1e-7 or u > 1.0 + 1e-7:
        return None
    q = _cross(s, e1)
    v = inv * _dot(direction, q)
    if v < -1e-7 or u + v > 1.0 + 1e-7:
        return None
    t = inv * _dot(e2, q)
    return t if t > 1e-7 else None

def _pt_strictly_inside_mesh(p, tris, tol=0.002):
    bound_tol = max(tol, 1e-6)
    for tri in tris:
        if _pt_tri_dist_sq(p, tri) <= bound_tol * bound_tol:
            return False
    hits = 0
    d_vec = (1.0, 1.37e-5, 2.71e-5)
    for tri in tris:
        t = _ray_hits_tri(p, d_vec, tri)
        if t is not None:
            hits += 1
    return (hits % 2) == 1

def _seg_tri_param(p0, p1, tri):
    direction = _sub(p1, p0)
    a, b, c = tri
    e1 = _sub(b, a)
    e2 = _sub(c, a)
    h = _cross(direction, e2)
    det = _dot(e1, h)
    if abs(det) < 1e-12:
        return None
    inv = 1.0 / det
    s = _sub(p0, a)
    u = inv * _dot(s, h)
    if u < -1e-6 or u > 1.0 + 1e-6:
        return None
    q = _cross(s, e1)
    v = inv * _dot(direction, q)
    if v < -1e-6 or u + v > 1.0 + 1e-6:
        return None
    t = inv * _dot(e2, q)
    return t if 0.0 <= t <= 1.0 else None

def _meshes_clash_gt_2mm(elem_a, elem_b, tol=0.002):
    ia = _aabb_overlap(elem_a["aabb"], elem_b["aabb"], tol=tol)
    if not ia:
        return False
    ix0 = max(elem_a["aabb"][0], elem_b["aabb"][0]) - 0.01
    iy0 = max(elem_a["aabb"][1], elem_b["aabb"][1]) - 0.01
    iz0 = max(elem_a["aabb"][2], elem_b["aabb"][2]) - 0.01
    ix1 = min(elem_a["aabb"][3], elem_b["aabb"][3]) + 0.01
    iy1 = min(elem_a["aabb"][4], elem_b["aabb"][4]) + 0.01
    iz1 = min(elem_a["aabb"][5], elem_b["aabb"][5]) + 0.01
    def _filter_tris(tris):
        kept = []
        for t in tris:
            if (
                max(t[0][0], t[1][0], t[2][0]) >= ix0 and min(t[0][0], t[1][0], t[2][0]) <= ix1
                and max(t[0][1], t[1][1], t[2][1]) >= iy0 and min(t[0][1], t[1][1], t[2][1]) <= iy1
                and max(t[0][2], t[1][2], t[2][2]) >= iz0 and min(t[0][2], t[1][2], t[2][2]) <= iz1
            ):
                kept.append(t)
        return kept
    ta = _filter_tris(elem_a["tris"])
    tb = _filter_tris(elem_b["tris"])
    if not ta or not tb:
        return False
    for p in elem_a["verts"]:
        if ix0 <= p[0] <= ix1 and iy0 <= p[1] <= iy1 and iz0 <= p[2] <= iz1:
            if _pt_strictly_inside_mesh(p, elem_b["tris"], tol=tol):
                return True
    for p in elem_b["verts"]:
        if ix0 <= p[0] <= ix1 and iy0 <= p[1] <= iy1 and iz0 <= p[2] <= iz1:
            if _pt_strictly_inside_mesh(p, elem_a["tris"], tol=tol):
                return True
    for src_edges, dst_tris, dst_all in (
        (elem_a["edges"], tb, elem_b["tris"]),
        (elem_b["edges"], ta, elem_a["tris"]),
    ):
        for p0, p1 in src_edges:
            if max(p0[0], p1[0]) < ix0 or min(p0[0], p1[0]) > ix1:
                continue
            if max(p0[1], p1[1]) < iy0 or min(p0[1], p1[1]) > iy1:
                continue
            if max(p0[2], p1[2]) < iz0 or min(p0[2], p1[2]) > iz1:
                continue
            ts = sorted(t for tri in dst_tris for t in [_seg_tri_param(p0, p1, tri)] if t is not None)
            pts = [0.0] + ts + [1.0]
            for k in range(len(pts) - 1):
                if pts[k + 1] - pts[k] <= 1e-6:
                    continue
                tm = 0.5 * (pts[k] + pts[k + 1])
                pm = (
                    p0[0] + tm * (p1[0] - p0[0]),
                    p0[1] + tm * (p1[1] - p0[1]),
                    p0[2] + tm * (p1[2] - p0[2]),
                )
                if _pt_strictly_inside_mesh(pm, dst_all, tol=tol):
                    return True
    return False

def _mesh_clearance(elem_a, elem_b, pad=0.35):
    best = _aabb_dist(elem_a["aabb"], elem_b["aabb"])
    if best >= pad:
        return best
    ba = elem_a["aabb"]
    bb = elem_b["aabb"]

    def _pt_near(p, box):
        return (
            box[0] - pad <= p[0] <= box[3] + pad
            and box[1] - pad <= p[1] <= box[4] + pad
            and box[2] - pad <= p[2] <= box[5] + pad
        )

    def _seg_near(p, q, box):
        return (
            max(p[0], q[0]) >= box[0] - pad and min(p[0], q[0]) <= box[3] + pad
            and max(p[1], q[1]) >= box[1] - pad and min(p[1], q[1]) <= box[4] + pad
            and max(p[2], q[2]) >= box[2] - pad and min(p[2], q[2]) <= box[5] + pad
        )

    def _tri_near(t, box):
        return (
            max(t[0][0], t[1][0], t[2][0]) >= box[0] - pad and min(t[0][0], t[1][0], t[2][0]) <= box[3] + pad
            and max(t[0][1], t[1][1], t[2][1]) >= box[1] - pad and min(t[0][1], t[1][1], t[2][1]) <= box[4] + pad
            and max(t[0][2], t[1][2], t[2][2]) >= box[2] - pad and min(t[0][2], t[1][2], t[2][2]) <= box[5] + pad
        )

    va = [p for p in elem_a["verts"] if _pt_near(p, bb)]
    vb = [p for p in elem_b["verts"] if _pt_near(p, ba)]
    ta = [t for t in elem_a["tris"] if _tri_near(t, bb)]
    tb = [t for t in elem_b["tris"] if _tri_near(t, ba)]
    ea = [(p, q) for p, q in elem_a["edges"] if _seg_near(p, q, bb)]
    eb = [(p, q) for p, q in elem_b["edges"] if _seg_near(p, q, ba)]

    min_d = pad
    for p in va:
        for tri in tb:
            d2 = _pt_tri_dist_sq(p, tri)
            if d2 < min_d * min_d:
                min_d = math.sqrt(d2)
                if min_d <= 1e-6:
                    return min_d
    for p in vb:
        for tri in ta:
            d2 = _pt_tri_dist_sq(p, tri)
            if d2 < min_d * min_d:
                min_d = math.sqrt(d2)
                if min_d <= 1e-6:
                    return min_d
    for p1, q1 in ea:
        for p2, q2 in eb:
            d = _seg_seg_dist(p1, q1, p2, q2)
            if d < min_d:
                min_d = d
                if min_d <= 1e-6:
                    return min_d
    return min_d

if ifc_files:
    try:
        import ifcopenshell
        import ifcopenshell.geom
    except ImportError:
        ifcopenshell = None
    try:
        from shapely.geometry import Polygon as _ShapPoly
        from shapely.ops import unary_union as _shap_union
        _SHAP = (_ShapPoly, _shap_union)
    except Exception:
        _SHAP = None

    if ifcopenshell is not None:
        settings = ifcopenshell.geom.settings()
        try:
            settings.set("use-world-coords", True)
        except Exception:
            try:
                settings.set(settings.USE_WORLD_COORDS, True)
            except Exception:
                pass

        all_elems = []
        building_groups = {}
        reported_clash_pairs = set()
        clash_count = 0

        def _extract_pset_text(prod, ifc_mod):
            parts = []
            try:
                util_mod = getattr(ifc_mod, "util", None)
                elem_mod = getattr(util_mod, "element", None) if util_mod else None
                get_psets = getattr(elem_mod, "get_psets", None) if elem_mod else None
                if callable(get_psets):
                    psets = get_psets(prod) or {}
                    if isinstance(psets, dict):
                        for pname, pvals in psets.items():
                            parts.append(str(pname))
                            if isinstance(pvals, dict):
                                for pk, pv in pvals.items():
                                    if pk != "id" and pv is not None:
                                        parts.append(f"{pk}={pv}")
                        if parts:
                            return " ".join(parts)
            except Exception:
                pass
            try:
                for rel_def in getattr(prod, "IsDefinedBy", None) or []:
                    pdef = getattr(rel_def, "RelatingPropertyDefinition", None)
                    if pdef is None:
                        continue
                    pname = getattr(pdef, "Name", None)
                    if pname:
                        parts.append(str(pname))
                    for prop in getattr(pdef, "HasProperties", None) or []:
                        k = getattr(prop, "Name", None)
                        val = getattr(prop, "NominalValue", None)
                        if val is not None:
                            val = getattr(val, "wrappedValue", val)
                        if k or val is not None:
                            parts.append(f"{k or ''}={val if val is not None else ''}")
            except Exception:
                pass
            return " ".join(parts)

        def _mep_service_bucket(e):
            txt = e["text"]
            if BUSWAY_RE.search(txt):
                return "busway"
            if e["cls"] in {"IfcCableCarrierSegment", "IfcCableCarrierFitting"} or TRAY_RE.search(txt) or TRAY_TAG_RE.search(txt):
                return "cable_tray"
            if e["cls"] in {"IfcPipeSegment", "IfcPipeFitting"} or CHW_PIPE_RE.search(txt):
                return "chw_pipe"
            return None

        def _is_mep(e):
            return (
                e["cls"] in MEP_CLASSES
                or _mep_service_bucket(e) is not None
                or (
                    e["cls"] in {"IfcBuildingElementProxy", "IfcCivilElement", "IfcProxy"}
                    and bool(MEP_NAME_RE.search(e["text"]))
                )
            )

        def _build_spatial_hash(elems, cell_size=GRID_CELL_M):
            grid = {}
            oversized = []
            bounds = []
            inv = 1.0 / cell_size
            for idx, e in enumerate(elems):
                a = e["aabb"]
                ix0, iy0, iz0 = math.floor(a[0] * inv), math.floor(a[1] * inv), math.floor(a[2] * inv)
                ix1, iy1, iz1 = math.floor(a[3] * inv), math.floor(a[4] * inv), math.floor(a[5] * inv)
                bounds.append((ix0, iy0, iz0, ix1, iy1, iz1))
                if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) * (iz1 - iz0 + 1) > 4096:
                    oversized.append(idx)
                    continue
                for ix in range(ix0, ix1 + 1):
                    for iy in range(iy0, iy1 + 1):
                        for iz in range(iz0, iz1 + 1):
                            grid.setdefault((ix, iy, iz), []).append(idx)
            return grid, oversized, bounds

        def _spatial_candidates(idx, grid, oversized, bounds, total_len):
            ix0, iy0, iz0, ix1, iy1, iz1 = bounds[idx]
            if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) * (iz1 - iz0 + 1) > 4096:
                out = set(range(total_len))
                out.discard(idx)
                return out
            cands = set(oversized)
            for ix in range(ix0 - 1, ix1 + 2):
                for iy in range(iy0 - 1, iy1 + 2):
                    for iz in range(iz0 - 1, iz1 + 2):
                        bucket = grid.get((ix, iy, iz))
                        if bucket:
                            cands.update(bucket)
            cands.discard(idx)
            return cands

        def _is_floor_rest(ra, rb):
            for eq, slab in ((ra, rb), (rb, ra)):
                if slab["cls"] == "IfcSlab" and not _is_mep(slab):
                    if abs(eq["aabb"][2] - slab["aabb"][5]) <= 0.001 and eq["aabb"][5] > slab["aabb"][5]:
                        return True
            return False

        def _emit_mep_clash(r1, r2, msg):
            global clash_count
            if r1 is None or r2 is None or (r1["file"], r1["gid"]) == (r2["file"], r2["gid"]):
                return False
            if _is_floor_rest(r1, r2):
                return False
            pkey = tuple(sorted(((r1["file"], r1["gid"]), (r2["file"], r2["gid"]))))
            if pkey in reported_clash_pairs:
                return False
            reported_clash_pairs.add(pkey)
            clash_count += 1
            if clash_count <= 15:
                errors.append(msg)
            return True

        def _rec_from_bvh_ref(ref, p_map, g_map):
            if ref is None:
                return None
            if isinstance(ref, int):
                return p_map.get(ref)
            rid = getattr(ref, "id", None)
            if callable(rid):
                try:
                    found = p_map.get(rid())
                    if found is not None:
                        return found
                except Exception:
                    pass
            elif isinstance(rid, int):
                found = p_map.get(rid)
                if found is not None:
                    return found
            rgid = getattr(ref, "GlobalId", None) or getattr(ref, "guid", None)
            if rgid:
                return g_map.get(str(rgid))
            return None

        for ifc_path in ifc_files:
            rel = _rel(ifc_path)
            try:
                model = ifcopenshell.open(str(ifc_path))
            except Exception as exc:
                errors.append(f"{rel}: failed to open IFC: {exc}")
                continue

            # Optional schema validation reported cleanly as warning
            try:
                import ifcopenshell.validate as _ifc_val
                _jlog = _ifc_val.json_logger()
                _ifc_val.validate(model, _jlog)
                _stmts = getattr(_jlog, "statements", None) or []
                if _stmts:
                    warnings.append(f"{rel}: ifcopenshell.validate reported {len(_stmts)} issue(s): {_stmts[0]}")
            except Exception:
                pass

            # Polyline & IfcIndexedPolyCurve duplicate consecutive vertex/index check
            try:
                for pc in model.by_type("IfcIndexedPolyCurve"):
                    pt_list = getattr(pc, "Points", None)
                    n_pts = len(getattr(pt_list, "CoordList", []) or []) if pt_list else 0
                    for seg in (getattr(pc, "Segments", None) or []):
                        if seg.is_a("IfcLineIndex"):
                            idxs = list(seg[0] or [])
                            if len(idxs) < 2:
                                errors.append(f"{rel}: {pc} has IfcLineIndex with < 2 indices")
                                break
                            for k in range(len(idxs)):
                                if n_pts and (idxs[k] < 1 or idxs[k] > n_pts):
                                    errors.append(f"{rel}: {pc} IfcLineIndex index {idxs[k]} out of bounds 1..{n_pts}")
                                    break
                                if k + 1 < len(idxs) and idxs[k] == idxs[k + 1]:
                                    errors.append(f"{rel}: {pc} has consecutive duplicate IfcLineIndex index {idxs[k]}")
                                    break
                for pl in model.by_type("IfcPolyline"):
                    pts = list(getattr(pl, "Points", None) or [])
                    if len(pts) < 2:
                        errors.append(f"{rel}: {pl} has < 2 points")
                        continue
                    for k in range(len(pts) - 1):
                        c1 = getattr(pts[k], "Coordinates", None)
                        c2 = getattr(pts[k + 1], "Coordinates", None)
                        if c1 and c2 and len(c1) == len(c2):
                            if math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(c1, c2))) <= 1e-9:
                                errors.append(f"{rel}: {pl} has zero-length consecutive duplicate point")
                                break
            except Exception as exc:
                warnings.append(f"{rel}: polyline check skipped: {exc}")

            # Map element id -> building id
            elem_to_bldg = {}
            bldg_labels = {}
            try:
                for b in model.by_type("IfcBuilding"):
                    bid = f"{rel}::{b.GlobalId}"
                    bldg_labels[bid] = f"{b.Name or b.GlobalId} ({rel})"
                    queue = [b]
                    visited = set()
                    while queue:
                        node = queue.pop()
                        if node.id() in visited:
                            continue
                        visited.add(node.id())
                        elem_to_bldg[node.id()] = bid
                        for agg in getattr(node, "IsDecomposedBy", []) or []:
                            for child in getattr(agg, "RelatedObjects", []) or []:
                                queue.append(child)
                        for relc in getattr(node, "ContainsElements", []) or []:
                            for child in getattr(relc, "RelatedElements", []) or []:
                                queue.append(child)
            except Exception:
                pass

            bvh_tree = None
            if hasattr(ifcopenshell.geom, "tree"):
                try:
                    bvh_tree = ifcopenshell.geom.tree()
                except Exception:
                    bvh_tree = None
            pid_to_rec = {}
            gid_to_rec = {}
            mep_prods = []
            all_prods = []

            for prod in model.by_type("IfcProduct"):
                if time.monotonic() - START_T > 25.0:
                    warnings.append("IFC geometry check stopped early at 25s time budget")
                    break
                cls = prod.is_a()
                if cls in NON_SOLID_CLASSES or cls in {"IfcBuildingStorey", "IfcSpatialZone"}:
                    continue
                if not getattr(prod, "Representation", None):
                    continue
                try:
                    shape = ifcopenshell.geom.create_shape(settings, prod)
                    raw_v = shape.geometry.verts
                    raw_f = shape.geometry.faces
                except Exception as exc:
                    errors.append(
                        f"{rel}: invalid/unmesheable IFC geometry for {cls} {getattr(prod, 'Name', '') or prod.id()}: {exc}"
                    )
                    continue
                if not raw_v or not raw_f or len(raw_v) % 3 != 0 or len(raw_f) % 3 != 0:
                    errors.append(
                        f"{rel}: empty/malformed mesh for {cls} {getattr(prod, 'Name', '') or prod.id()}"
                    )
                    continue
                if not all(math.isfinite(float(c)) for c in raw_v):
                    errors.append(
                        f"{rel}: non-finite (NaN/Inf) vertex coordinate in {cls} {getattr(prod, 'Name', '') or prod.id()}"
                    )
                    continue
                n_v = len(raw_v) // 3
                if n_v < 3 or any(int(idx) < 0 or int(idx) >= n_v for idx in raw_f):
                    errors.append(
                        f"{rel}: out-of-range face index or degenerate mesh in {cls} {getattr(prod, 'Name', '') or prod.id()}"
                    )
                    continue
                verts = [(float(raw_v[i]), float(raw_v[i + 1]), float(raw_v[i + 2])) for i in range(0, len(raw_v), 3)]
                tris = [
                    (verts[raw_f[i]], verts[raw_f[i + 1]], verts[raw_f[i + 2]])
                    for i in range(0, len(raw_f), 3)
                ]
                edge_set = set()
                for i in range(0, len(raw_f), 3):
                    a, b, c = raw_f[i], raw_f[i + 1], raw_f[i + 2]
                    for u, v in ((a, b), (b, c), (c, a)):
                        edge_set.add((min(u, v), max(u, v)))
                edges = [(verts[u], verts[v]) for u, v in edge_set]
                name = str(getattr(prod, "Name", "") or "")
                obj_t = str(getattr(prod, "ObjectType", "") or "")
                pset_t = _extract_pset_text(prod, ifcopenshell)
                gid = str(getattr(prod, "GlobalId", None) or prod.id())
                bid = elem_to_bldg.get(prod.id())
                rec = {
                    "file": rel,
                    "cls": cls,
                    "gid": gid,
                    "pid": prod.id(),
                    "name": name or gid,
                    "label": f"{cls} {name or gid}",
                    "pset_text": pset_t,
                    "text": f"{cls} {name} {obj_t} {pset_t}",
                    "bldg": bid,
                    "bldg_label": bldg_labels.get(bid, bid) if bid else None,
                    "verts": verts,
                    "tris": tris,
                    "edges": edges,
                    "aabb": _aabb_of(verts),
                }
                all_elems.append(rec)
                pid_to_rec[prod.id()] = rec
                gid_to_rec[gid] = rec
                all_prods.append(prod)
                if _is_mep(rec):
                    mep_prods.append(prod)
                if bvh_tree is not None:
                    try:
                        bvh_tree.add_element(shape)
                    except Exception:
                        pass
                if bid and cls != "IfcBuilding":
                    building_groups.setdefault(bid, []).append(rec)
                elif cls == "IfcBuilding":
                    building_groups.setdefault(bid or f"{rel}::{gid}", []).append(rec)

            # Per-model BVH clash checks (do NOT exempt IfcRelConnects* or 0mm butt-joints)
            if bvh_tree is not None and mep_prods:
                cim = getattr(bvh_tree, "clash_intersection_many", None)
                if cim is not None:
                    try:
                        sig = inspect.signature(cim)
                        pnames = set(sig.parameters.keys())
                        kw = {}
                        if "tolerance" in pnames:
                            kw["tolerance"] = 0.0
                        if "check_all" in pnames:
                            kw["check_all"] = True
                        if "allow_touching" in pnames:
                            kw["allow_touching"] = False
                        hits = cim(mep_prods, all_prods, **kw)
                    except Exception:
                        hits = []
                    for h in hits or []:
                        if isinstance(h, (tuple, list)) and len(h) >= 2:
                            r1 = _rec_from_bvh_ref(h[0], pid_to_rec, gid_to_rec)
                            r2 = _rec_from_bvh_ref(h[1], pid_to_rec, gid_to_rec)
                        else:
                            r1 = _rec_from_bvh_ref(
                                getattr(h, "a", getattr(h, "id1", getattr(h, "a_id", None))),
                                pid_to_rec,
                                gid_to_rec,
                            )
                            r2 = _rec_from_bvh_ref(
                                getattr(h, "b", getattr(h, "id2", getattr(h, "b_id", None))),
                                pid_to_rec,
                                gid_to_rec,
                            )
                        if r1 is not None and r2 is not None:
                            _emit_mep_clash(
                                r1,
                                r2,
                                f"MEP BVH clash (0mm/touching or overlapping): {r1['label']} ({r1['file']}) vs {r2['label']} ({r2['file']})",
                            )

                sel_fn = getattr(bvh_tree, "select", None)
                if sel_fn is not None:
                    for mp in mep_prods:
                        if time.monotonic() - START_T > 30.0:
                            break
                        r1 = pid_to_rec.get(mp.id())
                        if r1 is None:
                            continue
                        try:
                            matched = sel_fn(mp)
                        except Exception:
                            matched = []
                        for other in matched or []:
                            r2 = _rec_from_bvh_ref(other, pid_to_rec, gid_to_rec)
                            if r2 is not None:
                                _emit_mep_clash(
                                    r1,
                                    r2,
                                    f"MEP BVH clash (0mm/touching or overlapping): {r1['label']} ({r1['file']}) vs {r2['label']} ({r2['file']})",
                                )

        # Site checks: Road vs Building intersection & Building vs Building overlap
        roads = [
            e for e in all_elems
            if e["cls"] in {"IfcRoad", "IfcRoadPart"}
            or (e["bldg"] is None and ROAD_NAME_RE.search(e["text"]))
        ]
        bldg_aabbs = {}
        for bid, elems in building_groups.items():
            bldg_aabbs[bid] = (
                min(e["aabb"][0] for e in elems),
                min(e["aabb"][1] for e in elems),
                min(e["aabb"][2] for e in elems),
                max(e["aabb"][3] for e in elems),
                max(e["aabb"][4] for e in elems),
                max(e["aabb"][5] for e in elems),
            )

        for r in roads:
            for bid, b_elems in building_groups.items():
                ba = bldg_aabbs[bid]
                ra = r["aabb"]
                if min(ra[3], ba[3]) - max(ra[0], ba[0]) <= 0.01:
                    continue
                if min(ra[4], ba[4]) - max(ra[1], ba[1]) <= 0.01:
                    continue
                if ra[5] < ba[2] - 2.0 or ra[2] > ba[5] + 2.0:
                    continue
                b_tris = [t for e in b_elems for t in e["tris"]]
                area = _footprint_overlap_area(r["tris"], b_tris, _SHAP)
                if area > 0.01:
                    bname = b_elems[0]["bldg_label"] or bid
                    errors.append(
                        f"{r['file']}: road {r['name']!r} intersects building {bname!r} footprint (area={area:.3f} m^2)"
                    )

        bids = sorted(building_groups)
        for i in range(len(bids)):
            for j in range(i + 1, len(bids)):
                b1, b2 = bids[i], bids[j]
                a1, a2 = bldg_aabbs[b1], bldg_aabbs[b2]
                if not _aabb_overlap(a1, a2, tol=0.01):
                    continue
                t1 = [t for e in building_groups[b1] for t in e["tris"]]
                t2 = [t for e in building_groups[b2] for t in e["tris"]]
                area = _footprint_overlap_area(t1, t2, _SHAP)
                if area > 0.01:
                    l1 = building_groups[b1][0]["bldg_label"] or b1
                    l2 = building_groups[b2][0]["bldg_label"] or b2
                    errors.append(f"building overlap between {l1!r} and {l2!r} (area={area:.3f} m^2)")

        # MEP touching/overlapping clashes, tray-to-busway (<300mm), tray-stack headroom, clear height & connectivity
        mep_flags = [_is_mep(e) for e in all_elems]
        stack_errs = 0
        if any(mep_flags):
            grid, oversized, bounds = _build_spatial_hash(all_elems, cell_size=GRID_CELL_M)
            checked_pairs = set()
            for i in range(len(all_elems)):
                if time.monotonic() - START_T > 30.0:
                    warnings.append("MEP clash scan stopped at 30s time budget")
                    break
                if not mep_flags[i]:
                    continue
                ei = all_elems[i]
                bi = _mep_service_bucket(ei) == "busway"
                ti = _mep_service_bucket(ei) == "cable_tray"
                cands = _spatial_candidates(i, grid, oversized, bounds, len(all_elems))
                for j in sorted(cands):
                    pair = (i, j) if i < j else (j, i)
                    if pair in checked_pairs:
                        continue
                    checked_pairs.add(pair)
                    ej = all_elems[j]
                    pkey = tuple(sorted(((ei["file"], ei["gid"]), (ej["file"], ej["gid"]))))
                    if pkey not in reported_clash_pairs and not _is_floor_rest(ei, ej):
                        if mep_flags[j]:
                            if _aabb_dist(ei["aabb"], ej["aabb"]) < 0.002 and _mesh_clearance(ei, ej, pad=0.01) < 0.002:
                                _emit_mep_clash(
                                    ei,
                                    ej,
                                    f"MEP touching/overlapping clash (<2mm gap; leave 5-10mm gap at joints): "
                                    f"{ei['label']} ({ei['file']}) vs {ej['label']} ({ej['file']})",
                                )
                        elif (
                            (_aabb_dist(ei["aabb"], ej["aabb"]) < 0.001 and _mesh_clearance(ei, ej, pad=0.01) < 0.001)
                            or (_aabb_overlap(ei["aabb"], ej["aabb"], tol=0.0002) and _meshes_clash_gt_2mm(ei, ej, tol=0.0002))
                        ):
                            _emit_mep_clash(
                                ei,
                                ej,
                                f"MEP micro-clash (<1mm clearance or overlapping): {ei['label']} ({ei['file']}) vs {ej['label']} ({ej['file']})",
                            )
                    # Check tray <-> busway 300mm separation & tray-stack vertical headroom (with 1e-3mm epsilon)
                    bj = _mep_service_bucket(ej) == "busway"
                    tj = _mep_service_bucket(ej) == "cable_tray"
                    if (ti and bj) or (tj and bi):
                        tray_e, bus_e = (ei, ej) if (ti and bj) else (ej, ei)
                        if _aabb_dist(tray_e["aabb"], bus_e["aabb"]) < SEP_LIMIT_M:
                            clr = _mesh_clearance(tray_e, bus_e, pad=0.305)
                            if clr < SEP_LIMIT_M:
                                errors.append(
                                    f"tray-busway clearance {clr * 1000:.1f}mm < 300mm: {tray_e['label']} vs {bus_e['label']}"
                                )
                    if stack_errs < 15 and ((ti and mep_flags[j]) or (tj and mep_flags[i])):
                        tray_e, other_e = (ei, ej) if ti else (ej, ei)
                        ta, oa = tray_e["aabb"], other_e["aabb"]
                        xy_dx = min(ta[3], oa[3]) - max(ta[0], oa[0])
                        xy_dy = min(ta[4], oa[4]) - max(ta[1], oa[1])
                        if xy_dx > 0.01 and xy_dy > 0.01 and (oa[2] > ta[5] or ta[2] > oa[5]):
                            vgap = max(0.0, oa[2] - ta[5], ta[2] - oa[5])
                            if 0.0 < vgap < STACK_LIMIT_M:
                                stack_errs += 1
                                errors.append(
                                    f"tray-stack vertical headroom {vgap * 1000:.1f}mm < 300mm "
                                    f"(TIA-569-B access clearance): {tray_e['label']} vs {other_e['label']}"
                                )
            if clash_count > 15:
                errors.append(f"... plus {clash_count - 15} additional MEP solid clash(es)")

        # Per-file, per-service BFS connectivity check (<= 250.0 + 1e-3 mm) via spatial hash
        svc_groups = {}
        for e in all_elems:
            svc = _mep_service_bucket(e)
            if svc is not None:
                svc_groups.setdefault((e["file"], svc), []).append(e)
        for (rel_f, svc), svc_elems in sorted(svc_groups.items()):
            if len(svc_elems) < 2:
                continue
            sgrid, soversized, sbounds = _build_spatial_hash(svc_elems, cell_size=GRID_CELL_M)
            adj = {idx: set() for idx in range(len(svc_elems))}
            for u in range(len(svc_elems)):
                scands = _spatial_candidates(u, sgrid, soversized, sbounds, len(svc_elems))
                for v in scands:
                    if v <= u:
                        continue
                    if (
                        _aabb_dist(svc_elems[u]["aabb"], svc_elems[v]["aabb"]) <= CONNECTIVITY_TOL_M
                        and _mesh_clearance(svc_elems[u], svc_elems[v], pad=0.255) <= CONNECTIVITY_TOL_M
                    ):
                        adj[u].add(v)
                        adj[v].add(u)
            visited = set()
            comps = []
            for start in range(len(svc_elems)):
                if start in visited:
                    continue
                comp = []
                bfs_q = [start]
                visited.add(start)
                while bfs_q:
                    cur_node = bfs_q.pop(0)
                    comp.append(cur_node)
                    for nxt in adj[cur_node]:
                        if nxt not in visited:
                            visited.add(nxt)
                            bfs_q.append(nxt)
                comps.append(comp)
            if len(comps) > 1:
                comps.sort(key=len, reverse=True)
                main_comp = comps[0]
                rest_nodes = [n for c in comps[1:] for n in c]
                min_gap_m = min(
                    _aabb_dist(svc_elems[u]["aabb"], svc_elems[v]["aabb"])
                    for u in main_comp
                    for v in rest_nodes
                )
                detached_labels = [svc_elems[n]["label"] for n in rest_nodes[:4]]
                errors.append(
                    f"{rel_f}: disconnected MEP {svc} network ({len(comps)} separate components "
                    f"across {len(svc_elems)} elements, largest={len(main_comp)}, tol=250mm, "
                    f"min_gap={min_gap_m * 1000:.1f}mm; detached={detached_labels})"
                )

        # Per-service MEP length & elevation telemetry summary
        svc_elems_all = {}
        for e in all_elems:
            svc = _mep_service_bucket(e)
            if svc is not None:
                svc_elems_all.setdefault(svc, []).append(e)
        for svc, svc_list in sorted(svc_elems_all.items()):
            seg_lens = [
                max(
                    e["aabb"][3] - e["aabb"][0],
                    e["aabb"][4] - e["aabb"][1],
                    e["aabb"][5] - e["aabb"][2],
                )
                for e in svc_list
            ]
            total_len = round(sum(seg_lens), 3)
            seg_cnt = len(svc_list)
            mep_summary[svc] = {
                "length_m": total_len,
                "segment_count": seg_cnt,
                "avg_segment_m": round(total_len / seg_cnt, 3) if seg_cnt else 0.0,
                "z_min": round(min(e["aabb"][2] for e in svc_list), 4),
                "z_max": round(max(e["aabb"][5] for e in svc_list), 4),
            }

        # Overhead clear-height check (>= 2600mm AFF), separated by category & baseline origin
        TERMINAL_CLASSES = {
            "IfcFireSuppressionTerminal",
            "IfcAirTerminal",
            "IfcSanitaryTerminal",
            "IfcLightFixture",
            "IfcAudioVisualAppliance",
            "IfcCommunicationsAppliance",
            "IfcElectricAppliance",
            "IfcFlowTerminal",
        }

        def _is_terminal(e):
            return e["cls"] in TERMINAL_CLASSES or bool(SPRINKLER_RE.search(e["text"]))

        cat_warns = {"routed_mep": 0, "terminals": 0}
        for e in all_elems:
            is_term = _is_terminal(e)
            if _is_mep(e) or is_term:
                z_bot = e["aabb"][2]
                if 1.20 <= z_bot < 2.599:
                    cat = "terminals" if is_term else "routed_mep"
                    origin_tag = "[BASELINE_INPUT]" if e["gid"] in BASELINE_IFC_GIDS else "[MODIFIED/NEW]"
                    if len(clear_height[cat]) < 15:
                        clear_height[cat].append(
                            {
                                "label": e["label"],
                                "gid": e["gid"],
                                "file": e["file"],
                                "z_bot_mm": round(z_bot * 1000.0, 1),
                                "origin": origin_tag,
                            }
                        )
                    if cat_warns[cat] < 10:
                        cat_warns[cat] += 1
                        warnings.append(
                            f"[{cat}] {origin_tag} overhead clear height {z_bot * 1000:.1f}mm < 2600mm "
                            f"(check TIA-942 clear-height rule): {e['label']} ({e['file']})"
                        )

print(
    "__PLE_AUDIT_JSON__:"
    + json.dumps(
        {
            "errors": errors[:30],
            "warnings": warnings[:30],
            "pre_verifier_blockers": pre_verifier_blockers[:30],
            "missing_deliverables": missing_deliverables[:20],
            "skipped_unmodified_xlsx": skipped_unmodified_xlsx[:30],
            "skipped_unmodified_inputs": skipped_unmodified_inputs[:50],
            "mep_summary": mep_summary,
            "clear_height": clear_height,
        }
    )
)
'''


def _normalize_file_baseline(baseline_files: Optional[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """Validate and normalize a relative-path -> [size, sha256] file baseline map."""
    if not isinstance(baseline_files, dict):
        return {}
    clean: Dict[str, List[Any]] = {}
    for k, v in baseline_files.items():
        if isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                clean[k] = [int(v[0]), str(v[1])]
            except (TypeError, ValueError):
                continue
    return clean


def _normalize_xlsx_baseline(baseline_xlsx: Optional[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """Validate and normalize a relative-path -> [size, sha256] XLSX baseline map."""
    return _normalize_file_baseline(baseline_xlsx)


def _normalize_ifc_baseline(baseline_ifc_gids: Optional[Iterable[str]]) -> List[str]:
    """Validate and deduplicate pre-existing IFC GlobalId values."""
    if not baseline_ifc_gids or isinstance(baseline_ifc_gids, (str, bytes)):
        return []
    seen: set = set()
    clean: List[str] = []
    for gid in baseline_ifc_gids:
        s = str(gid or "").strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    return sorted(clean)[:50000]


async def capture_workspace_baseline(
    session: taskbridge.Session,
) -> Tuple[Dict[str, List[Any]], List[str]]:
    """Capture pre-existing auditable file hashes and IFC GlobalIds before WORKER runs."""
    if getattr(session, "cancelled", None) and session.cancelled.is_set():
        return {}, []
    if not hasattr(session, "exec"):
        return {}, []
    cmd = (
        "PY_BIN=$(command -v python3 || command -v python || true)\n"
        'if [ -n "$PY_BIN" ]; then\n'
        '  "$PY_BIN" - <<\'__PLE_XLSX_BASELINE__\'\n'
        f"{_XLSX_BASELINE_SCRIPT}\n"
        "__PLE_XLSX_BASELINE__\n"
        "fi\n"
    )
    try:
        cap = _safe_int(
            getattr(session, "limits", {}).get("exec_timeout_sec"),
            default=600,
            minimum=1,
        )
        res = await session.exec(cmd, timeout_sec=min(20, cap))
    except (taskbridge.TaskError, KeyError, TypeError, ValueError, RuntimeError, OSError):
        return {}, []
    if getattr(session, "cancelled", None) and session.cancelled.is_set():
        return {}, []

    output = str((res or {}).get("output") or "")
    file_base: Dict[str, List[Any]] = {}
    xlsx_base: Dict[str, List[Any]] = {}
    ifc_base: List[str] = []
    for line in reversed(output.splitlines()):
        if not file_base and line.startswith(FILE_BASELINE_MARKER):
            raw = line[len(FILE_BASELINE_MARKER):].strip()
            try:
                file_base = _normalize_file_baseline(json.loads(raw))
            except json.JSONDecodeError:
                file_base = {}
        elif not xlsx_base and line.startswith(XLSX_BASELINE_MARKER):
            raw = line[len(XLSX_BASELINE_MARKER):].strip()
            try:
                xlsx_base = _normalize_file_baseline(json.loads(raw))
            except json.JSONDecodeError:
                xlsx_base = {}
        elif not ifc_base and line.startswith(IFC_BASELINE_MARKER):
            raw = line[len(IFC_BASELINE_MARKER):].strip()
            try:
                parsed_ifc = json.loads(raw)
                if isinstance(parsed_ifc, list):
                    ifc_base = _normalize_ifc_baseline(parsed_ifc)
            except json.JSONDecodeError:
                ifc_base = []
    return (file_base or xlsx_base), ifc_base


async def capture_xlsx_baseline(session: taskbridge.Session) -> Dict[str, List[Any]]:
    """Capture a baseline snapshot of pre-existing XLSX/XLSM files in the task workspace."""
    file_base, _ = await capture_workspace_baseline(session)
    return {
        k: v
        for k, v in file_base.items()
        if Path(k).suffix.lower() in {".xlsx", ".xlsm"}
    }


def _normalize_required_register_ids(
    required_register_ids: Optional[Iterable[str]],
) -> List[str]:
    """Validate and deduplicate required register/requirement IDs."""
    if not required_register_ids or isinstance(required_register_ids, (str, bytes)):
        return []
    return _dedupe_strs(
        (str(x).strip() for x in required_register_ids if str(x).strip()),
        limit=200,
    )


def _normalize_target_deliverables(
    target_deliverables: Optional[Iterable[str]],
) -> List[str]:
    """Validate and deduplicate normalized declared deliverable paths."""
    if not target_deliverables or isinstance(target_deliverables, (str, bytes)):
        return []
    return _dedupe_strs(
        (
            str(x).strip().replace("\\", "/").lstrip("./").lstrip("/")
            for x in target_deliverables
            if str(x).strip()
        ),
        limit=100,
    )


def _build_programmatic_audit_script(
    task: str = "",
    baseline_xlsx: Optional[Dict[str, Any]] = None,
    baseline_ifc_gids: Optional[Iterable[str]] = None,
    baseline_files: Optional[Dict[str, Any]] = None,
    required_register_ids: Optional[Iterable[str]] = None,
    target_deliverables: Optional[Iterable[str]] = None,
    forbidden_citations: Optional[Iterable[str]] = None,
    allowed_sections: Optional[Iterable[str]] = None,
    disallowed_sections: Optional[Iterable[str]] = None,
    required_scope_exclusions: Optional[Iterable[str]] = None,
    enforce_deliverables: bool = False,
) -> str:
    """Return a self-contained Python 3 audit script for task deliverables."""
    _ = task
    script = _PROGRAMMATIC_AUDIT_SCRIPT
    clean_xlsx = _normalize_file_baseline(baseline_xlsx)
    if clean_xlsx:
        encoded = json.dumps(json.dumps(clean_xlsx, separators=(",", ":")))
        script = script.replace(
            "BASELINE_XLSX = {}  # __PLE_BASELINE_XLSX__",
            f"BASELINE_XLSX = json.loads({encoded})",
            1,
        )
    merged_files = dict(clean_xlsx)
    merged_files.update(_normalize_file_baseline(baseline_files))
    if merged_files:
        encoded_files = json.dumps(json.dumps(merged_files, separators=(",", ":")))
        script = script.replace(
            "BASELINE_FILES = {}  # __PLE_BASELINE_FILES__",
            f"BASELINE_FILES = json.loads({encoded_files})",
            1,
        )
    clean_gids = _normalize_ifc_baseline(baseline_ifc_gids)
    if clean_gids:
        encoded_gids = json.dumps(json.dumps(clean_gids, separators=(",", ":")))
        script = script.replace(
            "BASELINE_IFC_GIDS = set()  # __PLE_BASELINE_IFC_GIDS__",
            f"BASELINE_IFC_GIDS = set(json.loads({encoded_gids}))",
            1,
        )
    clean_rids = _normalize_required_register_ids(required_register_ids)
    if clean_rids:
        encoded_rids = json.dumps(json.dumps(clean_rids, separators=(",", ":")))
        script = script.replace(
            "REQUIRED_REGISTER_IDS = []  # __PLE_REQUIRED_REGISTER_IDS__",
            f"REQUIRED_REGISTER_IDS = json.loads({encoded_rids})",
            1,
        )
    clean_tdelivs = _normalize_target_deliverables(target_deliverables)
    if clean_tdelivs:
        encoded_tdelivs = json.dumps(json.dumps(clean_tdelivs, separators=(",", ":")))
        script = script.replace(
            "TARGET_DELIVERABLES = []  # __PLE_TARGET_DELIVERABLES__",
            f"TARGET_DELIVERABLES = json.loads({encoded_tdelivs})",
            1,
        )
    clean_forb = _normalize_required_register_ids(forbidden_citations)
    if clean_forb:
        encoded_forb = json.dumps(json.dumps(clean_forb, separators=(",", ":")))
        script = script.replace(
            "FORBIDDEN_CITATIONS = []  # __PLE_FORBIDDEN_CITATIONS__",
            f"FORBIDDEN_CITATIONS = json.loads({encoded_forb})",
            1,
        )
    clean_asec = _normalize_required_register_ids(allowed_sections)
    if clean_asec:
        encoded_asec = json.dumps(json.dumps(clean_asec, separators=(",", ":")))
        script = script.replace(
            "ALLOWED_SECTIONS = []  # __PLE_ALLOWED_SECTIONS__",
            f"ALLOWED_SECTIONS = json.loads({encoded_asec})",
            1,
        )
    clean_dsec = _normalize_required_register_ids(disallowed_sections)
    if clean_dsec:
        encoded_dsec = json.dumps(json.dumps(clean_dsec, separators=(",", ":")))
        script = script.replace(
            "DISALLOWED_SECTIONS = []  # __PLE_DISALLOWED_SECTIONS__",
            f"DISALLOWED_SECTIONS = json.loads({encoded_dsec})",
            1,
        )
    clean_excl = _normalize_required_register_ids(required_scope_exclusions)
    if clean_excl:
        encoded_excl = json.dumps(json.dumps(clean_excl, separators=(",", ":")))
        script = script.replace(
            "REQUIRED_SCOPE_EXCLUSIONS = []  # __PLE_REQUIRED_SCOPE_EXCLUSIONS__",
            f"REQUIRED_SCOPE_EXCLUSIONS = json.loads({encoded_excl})",
            1,
        )
    if enforce_deliverables:
        script = script.replace(
            "ENFORCE_DELIVERABLES = False  # __PLE_ENFORCE_DELIVERABLES__",
            "ENFORCE_DELIVERABLES = True",
            1,
        )
    return script


async def run_programmatic_audit(
    session: taskbridge.Session,
    task: str = "",
    baseline_xlsx: Optional[Dict[str, Any]] = None,
    baseline_ifc_gids: Optional[Iterable[str]] = None,
    baseline_files: Optional[Dict[str, Any]] = None,
    required_register_ids: Optional[Iterable[str]] = None,
    target_deliverables: Optional[Iterable[str]] = None,
    forbidden_citations: Optional[Iterable[str]] = None,
    allowed_sections: Optional[Iterable[str]] = None,
    disallowed_sections: Optional[Iterable[str]] = None,
    required_scope_exclusions: Optional[Iterable[str]] = None,
    enforce_deliverables: bool = True,
) -> Dict[str, Any]:
    """Run deterministic CSV/XLSX/JSON/DOCX/IFC checks in the task container before verification."""
    empty: Dict[str, Any] = {
        "errors": [],
        "warnings": [],
        "pre_verifier_blockers": [],
        "skipped_unmodified_xlsx": [],
        "skipped_unmodified_inputs": [],
        "mep_summary": {},
        "clear_height": {"routed_mep": [], "terminals": []},
        "summary": "",
    }
    if getattr(session, "cancelled", None) and session.cancelled.is_set():
        return empty
    if not hasattr(session, "exec"):
        return empty
    script = _build_programmatic_audit_script(
        task,
        baseline_xlsx=baseline_xlsx,
        baseline_ifc_gids=baseline_ifc_gids,
        baseline_files=baseline_files,
        required_register_ids=required_register_ids,
        target_deliverables=target_deliverables,
        forbidden_citations=forbidden_citations,
        allowed_sections=allowed_sections,
        disallowed_sections=disallowed_sections,
        required_scope_exclusions=required_scope_exclusions,
        enforce_deliverables=enforce_deliverables,
    )
    cmd = (
        "PY_BIN=$(command -v python3 || command -v python || true)\n"
        'if [ -n "$PY_BIN" ]; then\n'
        '  "$PY_BIN" - <<\'__PLE_AUDIT__\'\n'
        f"{script}\n"
        "__PLE_AUDIT__\n"
        "fi\n"
    )
    try:
        cap = _safe_int(
            getattr(session, "limits", {}).get("exec_timeout_sec"),
            default=600,
            minimum=1,
        )
        res = await session.exec(cmd, timeout_sec=min(90, cap))
    except (taskbridge.TaskError, KeyError, TypeError, ValueError, RuntimeError, OSError):
        return empty
    if getattr(session, "cancelled", None) and session.cancelled.is_set():
        return empty

    output = str((res or {}).get("output") or "")
    for line in reversed(output.splitlines()):
        if line.startswith(AUDIT_MARKER):
            raw = line[len(AUDIT_MARKER):].strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return empty
            if not isinstance(parsed, dict):
                return empty
            errors = [str(x) for x in (parsed.get("errors") or []) if str(x).strip()]
            warnings = [str(x) for x in (parsed.get("warnings") or []) if str(x).strip()]
            pre_verifier_blockers = [
                str(x) for x in (parsed.get("pre_verifier_blockers") or []) if str(x).strip()
            ]
            skipped_xlsx = [
                str(x) for x in (parsed.get("skipped_unmodified_xlsx") or []) if str(x).strip()
            ]
            skipped_inputs = [
                str(x) for x in (parsed.get("skipped_unmodified_inputs") or []) if str(x).strip()
            ]
            if not skipped_inputs and skipped_xlsx:
                skipped_inputs = list(skipped_xlsx)
            mep_summary = (
                parsed.get("mep_summary")
                if isinstance(parsed.get("mep_summary"), dict)
                else {}
            )
            raw_ch = parsed.get("clear_height")
            clear_height = (
                {
                    "routed_mep": list(raw_ch.get("routed_mep") or []),
                    "terminals": list(raw_ch.get("terminals") or []),
                }
                if isinstance(raw_ch, dict)
                else {"routed_mep": [], "terminals": []}
            )
            lines: List[str] = []
            if errors:
                lines.append("ERRORS (must be fixed; cannot PASS while non-empty):")
                lines.extend(f"- {e}" for e in errors[:20])
            if warnings:
                lines.append("WARNINGS (inspect carefully):")
                lines.extend(f"- {w}" for w in warnings[:15])
            if mep_summary:
                lines.append("MEP SUMMARY (keep routes compact; compare length vs. baseline):")
                for svc, stats in sorted(mep_summary.items()):
                    if isinstance(stats, dict):
                        lines.append(
                            f"- {svc}: length={stats.get('length_m', 0.0):.3f}m, "
                            f"segments={stats.get('segment_count', 0)}, "
                            f"avg_segment={stats.get('avg_segment_m', 0.0):.3f}m, "
                            f"z=[{stats.get('z_min', 0.0):.3f}, {stats.get('z_max', 0.0):.3f}]m"
                        )
            return {
                "errors": errors,
                "warnings": warnings,
                "pre_verifier_blockers": pre_verifier_blockers,
                "skipped_unmodified_xlsx": skipped_xlsx,
                "skipped_unmodified_inputs": skipped_inputs,
                "mep_summary": mep_summary,
                "clear_height": clear_height,
                "summary": "\n".join(lines),
            }
    return empty


async def solve(
    session: taskbridge.Session,
    *,
    client: Any = None,
    config_path: str = "submission.yaml",
    dry_run: bool = False,
) -> str:
    """Execute the Plan-Work-Verify loop for one PLE task session."""
    runtime_cfg = resolve_runtime_config(config_path)
    active_client = client
    if active_client is None:
        active_client = DryRunOpenAIClient() if dry_run else build_openai_client(runtime_cfg)

    breaker = CircuitBreaker(max_failures=runtime_cfg["max_failures"])
    total = session.time_left()

    def _until(share: float) -> float:
        return 0.0 if total is None else total * share

    def _time_for(wrap_up: float) -> bool:
        left = session.time_left()
        return left is None or left - wrap_up >= MIN_STEP_SEC

    try:
        mcp_registry, mcp_tool_defs, mcp_note = await connect_mcp(session)
        planner_tools = READ_ONLY + mcp_tool_defs
        worker_tools = TOOLS + mcp_tool_defs
        verifier_tools = READ_ONLY + mcp_tool_defs
        max_fix_rounds = int(runtime_cfg.get("max_fix_rounds", 2))

        task = session.instruction
        plan = await agent(
            session,
            conversation(PLANNER, session, task, mcp_note=mcp_note),
            planner_tools,
            _until(1.0 - PLAN_SHARE),
            client=active_client,
            runtime_cfg=runtime_cfg,
            mcp_registry=mcp_registry,
            breaker=breaker,
            max_turns=runtime_cfg.get("max_plan_turns", 18),
        )
        await session.log(f"plan: {plan[:1000]}")

        extract_prompt = f"## Task\n{task}\n\n## Plan\n{plan}"
        extract_turns = min(3, max(1, int(runtime_cfg.get("max_extract_turns", 3))))
        left_now = session.time_left()
        extract_timeout = 90.0 if left_now is None else min(90.0, max(5.0, float(left_now) * 0.15))
        try:
            raw_constraints = await asyncio.wait_for(
                agent(
                    session,
                    conversation(EXTRACTOR, session, extract_prompt, mcp_note=mcp_note),
                    planner_tools,
                    _until(1.0 - PLAN_SHARE - EXTRACT_SHARE),
                    client=active_client,
                    runtime_cfg=runtime_cfg,
                    mcp_registry=mcp_registry,
                    breaker=CircuitBreaker(max_failures=runtime_cfg["max_failures"]),
                    max_turns=extract_turns,
                    wrap_up_message=EXTRACTOR_WRAP_UP,
                    retry_waits=(2,),
                ),
                timeout=extract_timeout,
            )
            constraints_json = _parse_or_fallback_constraints(raw_constraints, task, plan)
        except (openai.APIError, asyncio.TimeoutError, RuntimeError, ValueError, TypeError) as exc:
            await session.log(f"extractor failed ({exc}); falling back to regex constraint extraction")
            constraints_json = json.dumps(_regex_extract_constraints(task, plan), indent=2, ensure_ascii=False)
        await session.log(f"constraints: {constraints_json[:1000]}")
        required_register_ids = _extract_required_register_ids(constraints_json, task, plan)
        target_deliverables = _extract_target_deliverables(
            constraints_json,
            workdir=str(getattr(session, "workdir", "") or ""),
        )
        excl_rules = extract_exclusion_rules(constraints_json, task, plan)

        baseline_files, baseline_ifc_gids = (
            await capture_workspace_baseline(session) if max_fix_rounds > 0 else ({}, [])
        )
        work = conversation(
            WORKER,
            session,
            f"{task}\n\n## Plan\n{plan}\n\n## Extracted Constraints (CONSTRAINTS.json)\n{constraints_json}",
            mcp_note=mcp_note,
        )
        work_state: Dict[str, Any] = {}
        summary = await agent(
            session,
            work,
            worker_tools,
            _until(CHECK_SHARE),
            client=active_client,
            runtime_cfg=runtime_cfg,
            mcp_registry=mcp_registry,
            breaker=breaker,
            final=True,
            max_turns=runtime_cfg.get("max_work_turns", 75),
            run_state=work_state,
        )
        if work_state.get("truncated"):
            await session.log(f"worker: truncated=True turns={work_state.get('turns')}")

        active_mcp_writes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for wr in work_state.get("mcp_writes") or []:
            tname = str(wr.get("tool") or "")
            wargs = wr.get("arguments") or {}
            active_mcp_writes[_mcp_write_target_key(tname, wargs)] = {"tool": tname, "arguments": wargs}

        audit = (
            await run_programmatic_audit(
                session,
                task,
                baseline_files=baseline_files,
                baseline_ifc_gids=baseline_ifc_gids,
                required_register_ids=required_register_ids,
                target_deliverables=target_deliverables,
                forbidden_citations=excl_rules["forbidden_citations"],
                allowed_sections=excl_rules["allowed_sections"],
                disallowed_sections=excl_rules["disallowed_sections"],
                required_scope_exclusions=excl_rules["required_scope_exclusions"],
            )
            if max_fix_rounds > 0
            else {
                "errors": [],
                "warnings": [],
                "pre_verifier_blockers": [],
                "skipped_unmodified_xlsx": [],
                "skipped_unmodified_inputs": [],
                "mep_summary": {},
                "clear_height": {"routed_mep": [], "terminals": []},
                "summary": "",
            }
        )
        if audit["errors"] or audit["warnings"]:
            await session.log(
                f"audit: errors={len(audit['errors'])} warnings={len(audit['warnings'])} "
                f"{'; '.join(audit['errors'][:5])}"
            )

        for _ in range(max_fix_rounds):
            if session.cancelled.is_set() or not _time_for(_until(CHECK_SHARE / 2)):
                break

            comment_prefilter_errors: List[str] = []
            for wr in active_mcp_writes.values():
                for txt_val in _extract_comment_texts_from_args(wr.get("arguments")):
                    for cf in detect_contradictory_comment_statuses(txt_val):
                        if cf not in comment_prefilter_errors:
                            comment_prefilter_errors.append(cf)

            review = (
                f"## Task\n{task}\n\n## Plan\n{plan}"
                f"\n\n## Extracted Constraints (CONSTRAINTS.json)\n{constraints_json}"
                f"\n\n## The engineer's summary\n{summary}"
            )
            if work_state.get("truncated"):
                review += (
                    "\n\n## Note\nWARNING: The engineer hit the step turn/time cap before "
                    "normal completion. Carefully scrutinize all deliverables on disk for "
                    "incomplete, stale, or unregenerated outputs."
                )
            if audit["summary"]:
                review += f"\n\n## Programmatic Pre-Check\n{audit['summary']}"
            if active_mcp_writes:
                written_list = sorted({str(w.get("tool") or "") for w in active_mcp_writes.values()})
                review += (
                    "\n\n## External-Store Readback Required\n"
                    f"Written MCP tools: {', '.join(written_list)}. "
                    "Read back the actual persisted MCP records/comments/journals and verify "
                    "single-valued Entity: Final_Status consistency against delivered files."
                )
                if comment_prefilter_errors:
                    review += (
                        "\nContradictory Status Pre-Filter Flags:\n"
                        + "\n".join(f"- {e}" for e in comment_prefilter_errors)
                    )

            pre_blockers = list(audit.get("pre_verifier_blockers") or [])
            audit_errors = list(audit.get("errors") or [])
            if pre_blockers:
                await session.log(
                    f"pre-verifier gate blocked: {len(pre_blockers)} deterministic blocker(s); skipping VERIFIER"
                )
                verdict = ""
                verifier_truncated = False
                readback_errors = []
                clean_pass = False
                all_gate_errors = list(dict.fromkeys(audit_errors + comment_prefilter_errors))
            else:
                verify_state: Dict[str, Any] = {}
                verdict = await agent(
                    session,
                    conversation(VERIFIER, session, review, mcp_note=mcp_note),
                    verifier_tools,
                    _until(CHECK_SHARE / 2),
                    client=active_client,
                    runtime_cfg=runtime_cfg,
                    mcp_registry=mcp_registry,
                    breaker=breaker,
                    max_turns=runtime_cfg.get("max_verify_turns", 25),
                    wrap_up_message=VERIFIER_WRAP_UP,
                    run_state=verify_state,
                )
                verifier_truncated = bool(verify_state.get("truncated"))
                readback_errors = check_mcp_readback_errors(
                    active_mcp_writes,
                    [str(r) for r in (verify_state.get("mcp_reads") or [])],
                    mcp_registry,
                )
                clean_pass, all_gate_errors = evaluate_pre_pass_gate(
                    verdict,
                    audit_errors=audit_errors,
                    prefilter_errors=comment_prefilter_errors,
                    readback_errors=readback_errors,
                    verifier_truncated=verifier_truncated,
                )
                await session.log(
                    f"verifier: truncated={verifier_truncated} audit_errors={len(audit_errors)} "
                    f"prefilter_errors={len(comment_prefilter_errors)} readback_errors={len(readback_errors)} "
                    f"{verdict[:1000]}"
                )
            if clean_pass or not _time_for(WRAP_UP_SEC):
                break
            if not pre_blockers and not verdict.strip() and not verifier_truncated and not all_gate_errors:
                break

            gap_sections: List[str] = []
            if audit_errors:
                gap_sections.append(
                    "Programmatic Pre-Check ERRORS (must fix):\n"
                    + "\n".join(f"- {err}" for err in audit_errors[:20])
                )
            if comment_prefilter_errors:
                gap_sections.append(
                    "Cross-Store Contradictory Comment Pre-Filter ERRORS (rewrite using clean Entity: Final_Status lines):\n"
                    + "\n".join(f"- {err}" for err in comment_prefilter_errors[:10])
                )
            if readback_errors:
                gap_sections.append(
                    "External-Store Readback ERRORS:\n"
                    + "\n".join(f"- {err}" for err in readback_errors)
                )
            if verdict.strip() and not is_pass_verdict(verdict):
                gap_sections.append(verdict)
            if verifier_truncated:
                gap_sections.append(
                    "Verifier ran out of turns/time before completing all checks; do NOT assume "
                    "completion. Re-verify every hard constraint, register/ID alignment, file path, "
                    "and BIM/IFC geometry clearance directly on disk."
                )
            gaps_text = "\n\n".join(gap_sections)

            work.append(
                {
                    "role": "user",
                    "content": (
                        "A reviewer checked your files and found these gaps:\n"
                        f"{gaps_text}\n\n"
                        "## Extracted Constraints (CONSTRAINTS.json)\n"
                        f"{constraints_json}\n\n"
                        "Fix them immediately by writing or updating and regenerating "
                        "every required deliverable file on disk (and amending any external "
                        "MCP records cleanly as `Entity: Final_Status`), preserve compact, "
                        "minimal-length BIM/MEP routes when resolving clash, separation, or "
                        "headroom gaps (use minimal local offsets, clean parallel lanes, or "
                        "valid elevation separation rather than long perimeter detours that "
                        "bloat total routed length), re-run verification checks on the "
                        "updated files before your turn budget runs out, then reply with "
                        "your verified summary."
                    ),
                }
            )
            fix_state: Dict[str, Any] = {}
            updated = await agent(
                session,
                work,
                worker_tools,
                WRAP_UP_SEC,
                client=active_client,
                runtime_cfg=runtime_cfg,
                mcp_registry=mcp_registry,
                breaker=breaker,
                final=True,
                max_turns=runtime_cfg.get("max_fix_turns", 30),
                run_state=fix_state,
            )
            if fix_state.get("truncated"):
                await session.log(f"fix: truncated=True turns={fix_state.get('turns')}")
            for wr in fix_state.get("mcp_writes") or []:
                tname = str(wr.get("tool") or "")
                wargs = wr.get("arguments") or {}
                active_mcp_writes[_mcp_write_target_key(tname, wargs)] = {"tool": tname, "arguments": wargs}
            summary = updated or summary
            work_state = fix_state
            audit = await run_programmatic_audit(
                session,
                task,
                baseline_files=baseline_files,
                baseline_ifc_gids=baseline_ifc_gids,
                required_register_ids=required_register_ids,
                target_deliverables=target_deliverables,
                forbidden_citations=excl_rules["forbidden_citations"],
                allowed_sections=excl_rules["allowed_sections"],
                disallowed_sections=excl_rules["disallowed_sections"],
                required_scope_exclusions=excl_rules["required_scope_exclusions"],
            )
            err_tail = f" {'; '.join(audit['errors'][:5])}" if audit["errors"] else ""
            await session.log(
                f"audit(post-fix): errors={len(audit['errors'])} warnings={len(audit['warnings'])}{err_tail}"
            )

        return summary or session.final_message
    except (taskbridge.Cancelled, asyncio.CancelledError):
        return session.final_message or "(stopped: time is up)"


async def start_ple_server(
    host: str = "0.0.0.0",
    port: Optional[int] = None,
    *,
    config_path: str = "submission.yaml",
    dry_run: bool = False,
    client: Any = None,
    handler: Any = None,
) -> Any:
    """Start and return a WebSocket server speaking official PLE Serve Protocol v1."""
    bind_port = (
        _safe_int(os.environ.get("PORT"), default=8765, minimum=1)
        if port is None
        else int(port)
    )

    async def _default_handler(session: taskbridge.Session) -> str:
        return await solve(
            session,
            client=client,
            config_path=config_path,
            dry_run=dry_run,
        )

    active_handler = handler or _default_handler

    async def _on_connect(ws: Any) -> None:
        await taskbridge._session(ws, active_handler)  # pylint: disable=protected-access

    return await _ws_serve(
        _on_connect,
        host,
        bind_port,
        max_size=taskbridge.MAX_FRAME,
        ping_interval=None,
    )


async def run_server(
    host: str = "0.0.0.0",
    port: Optional[int] = None,
    *,
    config_path: str = "submission.yaml",
    dry_run: bool = False,
    stop_event: Optional[asyncio.Event] = None,
    client: Any = None,
    handler: Any = None,
) -> None:
    """Serve PLE Protocol v1 connections until `stop_event` or a termination signal."""
    server = await start_ple_server(
        host=host,
        port=port,
        config_path=config_path,
        dry_run=dry_run,
        client=client,
        handler=handler,
    )
    bound_port = port if port is not None else 8765
    if getattr(server, "sockets", None):
        sock = server.sockets[0]
        if sock is not None:
            bound_port = sock.getsockname()[1]

    print(f"listening on {bound_port}", flush=True)
    logger.info(
        "PLE Serve Protocol v1 server listening on ws://%s:%d (dry_run=%s)",
        host,
        bound_port,
        dry_run,
    )

    active_stop = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(sig, active_stop.set)

    try:
        await active_stop.wait()
    finally:
        server.close()
        await server.wait_closed()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the PLE Serve Protocol v1 server."""
    default_port = _safe_int(os.environ.get("PORT"), default=8765, minimum=1)
    parser = argparse.ArgumentParser(description="Run the PLE Serve Protocol v1 server.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0).")
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Bind port (defaults to $PORT or 8765).",
    )
    parser.add_argument(
        "--config",
        default="submission.yaml",
        help="Path to submission.yaml (default: submission.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with offline DryRunOpenAIClient (no API key or proxy required).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entrypoint for launching the PLE Serve Protocol v1 server."""
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            run_server(
                host=args.host,
                port=args.port,
                config_path=args.config,
                dry_run=args.dry_run,
            )
        )


if __name__ == "__main__":
    main()
