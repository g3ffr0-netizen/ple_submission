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
    "to use, and concrete self-verification checks (for spreadsheets/CSVs: schema, "
    "header/row alignment, non-null IDs, totals; for IFC/BIM models: schema "
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
    "Before replying: (1)
