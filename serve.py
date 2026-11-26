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
PLANNER_WRAP_UP = {
    "role": "user",
    "content": (
        "Emit your complete step-by-step engineering plan now without calling another tool."
    ),
}
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
    "call view_image instead of writing pixel/ASCII OCR scripts (if cropping dense "
    "sheets to /tmp PNGs, note PIL may be absent so use matplotlib.image or zlib/struct, "
    "and ALWAYS include full outer margins x=0..W / y=0..H around perimeter walls so "
    "left/outer-margin callouts left of the West wall are never clipped; on MEP sheets "
    "E-101 and M-101 inspect busway BW-*, cable tray CT-*, and CHW-* service-entry "
    "callouts along the entire perimeter independently—never assume BW and CT share one "
    "entry coordinate—and record each service's exact (X, Y) mm service entry). Keep your "
    "inspection concise so you stay well within your turn budget, then reply "
    "with the plan: each deliverable with its exact path and format, every hard "
    "constraint / threshold / register or tag ID format / per-service (X, Y) mm entry, the data and method "
    "validation, unstacked side-by-side overhead lateral X/Y lanes >= 300 mm apart inside [2600, 3050] mm "
    "for cable_tray (Z_bot in [2605, 2620] mm so Z_top <= 2750 mm keeps >= 300 mm soffit headroom) and busway "
    "(Z_bot >= 2605 mm)—treating A-301/A-602 low-soffit/sprinkler zones as localized X/Y keepouts rather "
    "than dumping trunks under-floor Z < 0—a single shared Manhattan main trunk + perpendicular row taps "
    "keeping total length <= 1.15 * optimal Manhattan Steiner length (<= 220 m total / <= 85 m per service), ifcopenshell.geom.tree BVH "
    "clash checks across all elements with 5-10 mm non-touching joint gaps, per-service <= 2500 mm service-entry reach, coordinate clearances, and boundary checks)."
    "validation, ifcopenshell.geom.tree BVH clash checks across all elements with "
    "5-10 mm non-touching joint gaps, per-service <= 2500 mm service-entry reach, coordinate clearances, and boundary checks)."
)

EXTRACTOR = (
    "You extract strict engineering constraints and verification requirements from a "
    "task and its plan. Do not create or edit files or spend more than 2 tool turns "
    "(working dir {workdir}); if the prompt and plan already supply the constraints, "
    "reply immediately with a single valid JSON object representing CONSTRAINTS.json "
    "containing keys: deliverables (exact file paths and external MCP targets), "
    "required_register_ids (every requirement, finding, hazard, or scenario ID such as "
    "R01..R99, R-xxxxxx-xx, R-ILLUM, H01.., S01.. referenced in the task, plan, or spec), "
    "service_entries_mm (dict mapping 'busway', 'cable_tray', 'chw_pipe' to [x_mm, y_mm] "
    "whenever MEP service-entry coordinates are stated in the task, plan, or drawings), "
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
    "preserving positive margins with >= 0.5% buffer where feasible); (2) if creating or modifying .ifc BIM deliverables, "
    "(a) if drawing PNG sheets (E-101, M-101, A-101, F-101) specify service-entry locations, crop full-margin perimeter "
    "strips (for West wall include x=0..0.45*W across full height y=0..H so callout text in the left margin is NEVER clipped; "
    "remember PNG y=0 is North Y_max and y=H is South Y=0, so lower-West offset entries like (200, 4910) appear at y~0.45*H..0.75*H "
    "separate from upper-West entries near (0, 12470)), inspect BW-* busway and CT-* cable tray on E-101 and CHW-* on M-101 "
    "individually with view_image (governing discipline sheets E-101/M-101 override generic notes when inter-sheet offsets exist; "
    "never assume busway, cable tray, and CHW share one entry coordinate), record exact `\"service_entries_mm\": {{\"busway\": [x, y], "
    "\"cable_tray\": [x, y], \"chw_pipe\": [x, y]}}` in `output/reconciliation.json` (or `output/service_entries.json`), and ensure "
    "every routed network starts within <= 2500 mm XY of its own drawn service entry, forms 1 connected component (<= 250 mm joint "
    "tolerance), reaches >= 80% of every rack row (<= 2500 mm XY), and reaches every CRAH unit (<= 2500 mm XY for CHW); (b) run "
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
    "non-intersection). CRITICAL MEP ELEVATION & LENGTH RULE: NEVER dump "
    "cable_tray or busway horizontal trunks into the under-floor plenum (Z < 0) "
    "just because vertical stacking of tray + busway + 460 mm sprinkler standoff "
    "exceeds soffit-to-2600 mm; routing horizontal trunks at Z < 0 drops "
    "fraction_in_band to 0.0 and triggers clear_height + tray_stack_headroom "
    "violations while bloating length! Instead, place cable_tray and busway "
    "UNSTACKED side-by-side in separate lateral X/Y lanes (>= 300 mm horizontal "
    "edge-to-edge separation) inside the overhead [2600, 3050] mm clear-height "
    "band: set cable_tray Z_bot in [2605, 2620] mm (so a 127 mm tray sits at "
    "Z_top <= 2750 mm, preserving >= 300 mm headroom below a 3050 mm soffit) and "
    "place busway in an adjacent parallel X/Y lane at Z_bot >= 2605 mm, bypassing "
    "localized low-soffit or sprinkler keepouts locally with short X/Y doglegs. "
    "Keep chw_pipe in its valid corridor with vertical drops only at terminals. "
    "Use a single shared Manhattan main trunk + direct perpendicular row taps "
    "(never a wide non-crossing comb) so total routed length stays compact "
    "(within <= 15% of optimal Manhattan Steiner length, <= 220 m combined / <= 85 m per service). "
    "place cable_tray and busway UNSTACKED side-by-side in separate lateral X/Y "
    "lanes (>= 300 mm horizontal separation) inside the overhead [2600, 3050] mm "
    "clear-height band (Z_bot >= 2620 mm, bypassing localized low-soffit or "
    "sprinkler keepouts locally in X/Y), and route CHW in its valid corridor with "
    "short vertical drops only at terminals. Use a single shared Manhattan main "
    "trunk + direct perpendicular row taps (never a wide non-crossing comb) so "
    "total routed length stays compact (within <= 15% of optimal Manhattan Steiner length). "
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
    "count is 0 while each service forms 1 connected component <= 250 mm), crop full-margin "
    "perimeter strips (x=0..0.45*W, y=0..H plus other borders—never rely on uncropped full-sheet views) "
    "of E-101.png and M-101.png to verify separate BW-*, CT-*, and CHW-* service-entry (X, Y) coordinates "
    "against reconciliation.json['service_entries_mm'] and MEP SUMMARY feed_endpoints_mm (<= 2500 mm XY), "
    "verify rack-row coverage (>= 80% per row <= 2500 mm), and CRAH reachability, plus 3D "
    "clearances (tray-to-busway >= 300 mm, tray-stack headroom >= 300 mm, overhead "
   "clear height >= 2600 mm AFF, sprinkler >= 457 mm, PDU clear zones, road vs. "
    "building non-intersection), reject if > 25% of horizontal cable_tray or busway "
    "length is dumped under-floor (Z < 0) when [2600, 3050] mm overhead lanes "
    "are available, if overhead cable_tray exceeds z_top > 2750 mm (< 300 mm soffit "
    "headroom), or if MEP length shows non-minimal comb detours (> 220 m combined "
    "or > 85 m per service), and inspect every Programmatic Pre-Check warning "
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
    read_file_cache: Optional[Dict[str, Tuple[str, int, str]]] = None,
    current_call_id: str = "",
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
            text_val = data.decode("utf-8", "replace") or "(empty file)"
            digest = hashlib.sha256(data).hexdigest()[:16]
            if read_file_cache is not None:
                prev_entry = read_file_cache.get(resolved)
                if prev_entry is not None and prev_entry[0] == digest:
                    return (
                        f"INFO: {args['path']} ({len(data)} bytes, sha256={digest}) is unchanged "
                        "since your earlier read in this conversation; reuse its contents from "
                        "context above."
                    )
                read_file_cache[resolved] = (digest, len(data), current_call_id)
            return clip(text_val)

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
            if read_file_cache is not None:
                read_file_cache.pop(resolved, None)
            return f"wrote {size} bytes to {args['path']}"

        if name == "edit_file":
            resolved = _resolve_path(session, args["path"])
            edit_res = await edit_file_in_session(
                session,
                args["path"],
                args["old_text"],
                args["new_text"],
                replace_all=bool(args.get("replace_all", False)),
            )
            if read_file_cache is not None and not str(edit_res).startswith("error:"):
                read_file_cache.pop(resolved, None)
            return edit_res

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
    svc_entry_hints = extract_mep_service_entry_hints(task, plan=plan)
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
    if svc_entry_hints:
        out["service_entries_mm"] = {
            svc: [[int(round(pt[0] * 1000.0)), int(round(pt[1] * 1000.0))] for pt in pts]
            for svc, pts in svc_entry_hints.items()
        }
    if not (
        file_paths
        or required_register_ids
        or hard_numeric_caps
        or numeric_thresholds
        or iso_dates
        or entities
        or empty_set_triggers
        or svc_entry_hints
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
    read_file_cache: Dict[str, Tuple[str, int, str]] = {}
    last_failed_bash_sig: Optional[str] = None

    def _evict_pruned_read_cache() -> None:
        if not read_file_cache:
            return
        unpruned_ids = {
            str(m.get("tool_call_id") or "")
            for m in messages
            if m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and not m["content"].startswith("[pruned ")
            and "\n[... cut to save context; run the tool again if needed]" not in m["content"]
        }
        for cached_path in [p for p, entry in read_file_cache.items() if entry[2] not in unpruned_ids]:
            read_file_cache.pop(cached_path, None)

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
                _evict_pruned_read_cache()
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
                _evict_pruned_read_cache()
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
                    b_cmd
