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
import json
import logging
import os
import posixpath
import re
import signal
import sys
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import openai
from websockets.asyncio.server import serve as _ws_serve

import taskbridge
from harness import (
    CircuitBreaker,
    DEFAULT_MODEL,
    _safe_int,
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
MAX_IMAGE_BYTES = 5 << 20
MAX_ACTIVE_IMAGES = 4
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
CHECK_SHARE = 0.25
MIN_STEP_SEC = 120
DIRECT_MCP_CHARS = 40_000
MAX_TOOLS = 128

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

WORKER = (
    "You are an expert engineer working on a task in a Linux container with "
    "no internet access. Your tools act inside it, and bash starts in "
    "{workdir}. A plan for the task follows it; use it, and correct it where "
    "the files show it is wrong. For image files (drawings, schedules, plots), "
    "use view_image rather than pixel/ASCII OCR scripts (if a sheet is dense, "
    "crop/zoom regions to a PNG under /tmp and inspect with view_image). "
    "Compute with code, not by hand. Watch the [Turn X/Y] pacing indicator "
    "on tool outputs: never spend most of your budget exploring while leaving "
    "placeholder stubs untouched—write a working version of every required "
    "deliverable file on disk before half your turns are spent, then iterate "
    "and verify. Before replying: (1) programmatically verify every numerical "
    "hard constraint, threshold, summation, and cross-table ID / register / tag "
    "consistency (zero missing/shifted columns, zero NaN/N/A/TBD values or "
    "formula errors); (2) if creating or modifying .ifc BIM deliverables, run "
    "ifcopenshell.validate plus ifcopenshell.geom.tree BVH clash checks across "
    "ALL elements (including same-service MEP segments—never exempt "
    "IfcRelConnects* links or 0 mm touching butt-joints). Because BVH flags "
    "0 mm touching faces as hard clashes, trim L-corners, T-junctions, and "
    "inline segment joints to leave clean 5-10 mm non-touching air gaps (well "
    "within connectivity joint tolerance such as 250 mm) so BVH reports zero "
    "clashes while keeping full graph connectivity, and verify all code "
    "clearances from tessellated 3D geometry (tray-to-busway >= 300 mm, "
    "tray-stack/access headroom >= 300 mm above/below trays per TIA-569-B, "
    "overhead clear height >= 2600 mm AFF unless explicitly permitted, "
    "sprinkler clearance >= 457 mm, PDU clear zones, and site building/road "
    "non-intersection). After any edit or fix, immediately regenerate and re-run "
    "verification checks on the actual deliverable file before replying. When "
    "done, reply without calling a tool: summarize each deliverable, its exact "
    "path, and the verified results."
)

VERIFIER = (
    "You check an engineer's work on a task in a Linux container. You get the "
    "task, the plan, the engineer's summary of what was delivered, and an "
    "automated Programmatic Pre-Check summary. Open and check every required "
    "deliverable with your tools (bash starts in {workdir}; use view_image "
    "when inspecting images or rendered plots) against the task: verify each "
    "deliverable file exists on disk, is NOT an unmodified template/stub, "
    "reflects the latest script edits, matches the exact path, headers, and "
    "format, has no shifted CSV columns, formula errors, duplicate rows, or "
    "missing/unpopulated register/device IDs, and satisfies every hard "
    "constraint and tolerance recomputed directly from the file. For .ifc "
    "deliverables, run Python checks using ifcopenshell.geom.tree BVH across "
    "ALL elements without exempting IfcRelConnects* or 0 mm butt-joints "
    "(verify 5-10 mm non-touching gaps at L-corners/T-junctions/joints so "
    "BVH clash count is 0 while connectivity holds) plus 3D clearances "
    "(tray-to-busway >= 300 mm, tray-stack headroom >= 300 mm, overhead clear "
    "height >= 2600 mm AFF, sprinkler >= 457 mm, PDU clear zones, road vs. "
    "building non-intersection) rather than trusting the summary. Do not "
    "create or change any file. If every requirement and pre-check passes with "
    "zero errors, reply with the single word PASS and nothing else. Otherwise "
    "reply with a numbered list of every gap, each specific enough to fix."
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
    if "generativelanguage.googleapis.com" in cleaned.lower():
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
    max_work_turns = _safe_int(
        os.environ.get("MAX_WORK_TURNS") or env_cfg.get("MAX_WORK_TURNS"),
        default=55,
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

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "effort": effort,
        "max_output_tokens": max_output_tokens,
        "max_fix_rounds": max_fix_rounds,
        "max_retries": max_retries,
        "max_failures": max_failures,
        "max_plan_turns": max_plan_turns,
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
                        "text": "[Earlier image omitted to save context; call view_image again if needed]",
                    }
                    pruned += 1
    return pruned


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
            return clip(f"exit code {result['exit_code']}{note}\n{result.get('output') or ''}")

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
            b64 = base64.b64encode(data).decode("ascii")
            if image_sink is not None:
                image_sink.append(
                    {
                        "path": args["path"],
                        "mime": mime,
                        "b64": b64,
                        "size": len(data),
                    }
                )
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
    left = session.time_left()
    if left is not None and left > until:
        wait = min(wait, max(0.0, left - until))
    await session.log(
        f"model call failed ({err_text[:200]}); retrying in {wait:.0f} s"
    )
    await asyncio.sleep(wait)
    return True


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
) -> str:
    """Run tool-calling loop on `messages` until the model produces a final reply."""
    model = runtime_cfg["model"]
    max_output_tokens = int(runtime_cfg["max_output_tokens"])
    cache_mark = bool(runtime_cfg.get("cache_mark", False))
    call_opts: Dict[str, Any] = {
        "token_param": "max_completion_tokens",
        "effort": runtime_cfg.get("effort"),
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
            }
        )

    nudges, head, waits, turns = 0, "", iter(RETRY_WAITS), 0
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

        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": marked(messages, cache_mark),
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
                model,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                getattr(details, "cached_tokens", 0) or 0,
            )

        choice = reply.choices[0]
        message = choice.message
        cut = getattr(choice, "finish_reason", None) == "length"
        msg_content = getattr(message, "content", None) or ""
        text = head + msg_content
        if final and text:
            session.final_message = text

        turn = (
            message.model_dump(exclude_none=True)
            if hasattr(message, "model_dump")
            else {"role": "assistant", "content": msg_content or None}
        )
        tool_calls = getattr(message, "tool_calls", None) or []

        if wrap_up or not tool_calls:
            turn.pop("tool_calls", None)
            if msg_content:
                messages.append(turn)
            if wrap_up or (msg_content and not cut) or nudges == MAX_NUDGES:
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
        messages.append(turn)

        serialized_calls = turn.get("tool_calls") or tool_calls
        tripped = False
        pending_images: List[Dict[str, Any]] = []
        for call in serialized_calls:
            call_id = (
                call.get("id")
                if isinstance(call, dict)
                else getattr(call, "id", "call_0")
            )
            fn = _extract_fn(call)
            await session.log(
                f"{fn.get('name', '')} {str(fn.get('arguments', ''))[:200]}"
            )
            content = await run_tool(
                session,
                fn,
                mcp_registry=mcp_registry,
                image_sink=pending_images,
            )
            if content.startswith("error:"):
                if tool_breaker.record_failure():
                    tripped = True
            else:
                tool_breaker.record_success()
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
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{img['mime']};base64,{img['b64']}"},
                    }
                )
            messages.append({"role": "user", "content": parts})
            _prune_old_images(messages, max_images=MAX_ACTIVE_IMAGES)

        if tripped:
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

_PROGRAMMATIC_AUDIT_SCRIPT = r'''
import csv
import inspect
import json
import math
import os
import re
import time
from pathlib import Path

START_T = time.monotonic()
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
ZERO_BYTE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".json", ".ifc", ".pdf", ".docx"}
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
BUSWAY_RE = re.compile(r"(?:^|[^a-z0-9])(busway|bus_?duct|bus-duct|bus)(?:[^a-z0-9]|$)", re.I)
MEP_NAME_RE = re.compile(r"(tray|busway|bus_?duct|conduit|pipe|duct|chilled|cable)", re.I)
SPRINKLER_RE = re.compile(r"(sprinkler|(?:^|[^a-z0-9])spr[-_])", re.I)
SUMMARY_ROW_RE = re.compile(
    r"^(total|subtotal|grand\s+total|summary|sum|average|avg|mean|min|max|note|notes)\b",
    re.I,
)

errors = []
warnings = []
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

ifc_files = []
for p in files:
    rel = _rel(p)
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        continue
    if ext in ZERO_BYTE_EXTS and size == 0:
        errors.append(f"{rel}: deliverable file is empty (0 bytes)")
        continue
    if ext == ".ifc":
        ifc_files.append(p)
        continue
    if size > 25 * 1024 * 1024:
        continue

    if ext == ".csv":
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            if text.lstrip().startswith("```"):
                errors.append(f"{rel}: CSV starts with markdown code fence")
                continue
            rows = list(csv.reader(text.splitlines()))
        except Exception as exc:
            errors.append(f"{rel}: CSV parse error: {exc}")
            continue
        if not rows:
            errors.append(f"{rel}: empty CSV file")
            continue
        header = rows[0]
        if not header or all(not str(c).strip() for c in header):
            errors.append(f"{rel}: blank CSV header row")
            continue
        data_rows = [r for r in rows[1:] if any(str(c).strip() for c in r)]
        bad_cols = [idx + 2 for idx, r in enumerate(rows[1:]) if r and len(r) != len(header)]
        if bad_cols:
            errors.append(
                f"{rel}: shifted/malformed CSV columns on {len(bad_cols)} row(s) "
                f"(first at row {bad_cols[0]}: expected {len(header)} cols)"
            )
        if not data_rows:
            warnings.append(f"{rel}: CSV has header only (0 data rows)")
            continue
        valid_rows = [r for r in data_rows if len(r) == len(header)]
        norm_headers = [_norm_col(h) for h in header]
        seen_rows = set()
        dup_row_count = 0
        for r_idx, r in enumerate(valid_rows, start=2):
            row_key = tuple(c.strip() for c in r)
            if row_key in seen_rows:
                dup_row_count += 1
            else:
                seen_rows.add(row_key)
            for c_idx, cell in enumerate(r):
                tok = cell.strip()
                if tok.upper() in FORMULA_ERRORS or tok.lower() in NAN_TOKENS:
                    errors.append(
                        f"{rel}: invalid numeric/formula token {tok!r} at row {r_idx}, col {header[c_idx]!r}"
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
        if id_rows:
            for c_idx, nh in enumerate(norm_headers):
                if nh not in PRIMARY_ID_COLS:
                    continue
                vals = [r[c_idx].strip() for r in id_rows]
                missing = [i + 2 for i, v in enumerate(vals) if v.lower() in MISSING_ID_TOKENS]
                has_placeholder = any(vals[i - 2].lower() in PLACEHOLDER_TOKENS for i in missing)
                if missing and (len(missing) < len(vals) or has_placeholder or nh in STRICT_ID_COLS):
                    errors.append(
                        f"{rel}: column {header[c_idx]!r} has missing/placeholder IDs on {len(missing)}/{len(vals)} non-summary row(s) "
                        f"(first at row {missing[0]})"
                    )

    elif ext in {".xlsx", ".xlsm"}:
        try:
            import openpyxl
        except ImportError:
            openpyxl = None
        if openpyxl is not None:
            for data_only in (False, True):
                wb = None
                try:
                    wb = openpyxl.load_workbook(p, read_only=True, data_only=data_only)
                    for sname in wb.sheetnames:
                        ws = wb[sname]
                        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                            if r_idx > 20000:
                                break
                            for c_idx, val in enumerate(row, start=1):
                                if val is None:
                                    continue
                                if isinstance(val, float) and not math.isfinite(val):
                                    errors.append(f"{rel}[{sname}] R{r_idx}C{c_idx}: non-finite float {val}")
                                elif isinstance(val, str):
                                    s = val.strip()
                                    if s.upper() in FORMULA_ERRORS or s.lower() in NAN_TOKENS:
                                        errors.append(f"{rel}[{sname}] R{r_idx}C{c_idx}: Excel error/NaN token {s!r}")
                except Exception as exc:
                    errors.append(f"{rel}: failed reading workbook (data_only={data_only}): {exc}")
                finally:
                    if wb is not None:
                        try:
                            wb.close()
                        except Exception:
                            pass

    elif ext == ".json" and p.name.lower() not in SKIP_JSON_NAMES:
        try:
            parsed = json.loads(p.read_text(encoding="utf-8", errors="strict"), parse_constant=_reject_const)
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
                elif isinstance(cur_val, str) and cur_val.strip().upper() in FORMULA_ERRORS:
                    errors.append(f"{rel}: spreadsheet error string {cur_val!r} in JSON")
                    break
        except Exception as exc:
            errors.append(f"{rel}: invalid JSON or non-finite numeric literal: {exc}")

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
    for tri in tris:
        if _pt_tri_dist_sq(p, tri) <= tol * tol:
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

def _mesh_clearance(elem_a, elem_b):
    best = _aabb_dist(elem_a["aabb"], elem_b["aabb"])
    if best >= 0.35:
        return best
    pad = 0.35
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
                if min_d < 0.299:
                    return min_d
    for p in vb:
        for tri in ta:
            d2 = _pt_tri_dist_sq(p, tri)
            if d2 < min_d * min_d:
                min_d = math.sqrt(d2)
                if min_d < 0.299:
                    return min_d
    for p1, q1 in ea:
        for p2, q2 in eb:
            d = _seg_seg_dist(p1, q1, p2, q2)
            if d < min_d:
                min_d = d
                if min_d < 0.299:
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

        def _is_mep(e):
            return e["cls"] in MEP_CLASSES or (
                e["cls"] in {"IfcBuildingElementProxy", "IfcCivilElement", "IfcProxy"}
                and bool(MEP_NAME_RE.search(e["text"]))
            )

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
                except Exception:
                    continue
                if not raw_v or not raw_f:
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
                gid = str(getattr(prod, "GlobalId", None) or prod.id())
                bid = elem_to_bldg.get(prod.id())
                rec = {
                    "file": rel,
                    "cls": cls,
                    "gid": gid,
                    "pid": prod.id(),
                    "name": name or gid,
                    "label": f"{cls} {name or gid}",
                    "text": f"{cls} {name} {obj_t}",
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
                if min(ra[3], ba[3]) - max(ra[0], ba[0]) <= 0.05:
                    continue
                if min(ra[4], ba[4]) - max(ra[1], ba[1]) <= 0.05:
                    continue
                if ra[5] < ba[2] - 2.0 or ra[2] > ba[5] + 2.0:
                    continue
                b_tris = [t for e in b_elems for t in e["tris"]]
                area = _footprint_overlap_area(r["tris"], b_tris, _SHAP)
                if area > 0.05:
                    bname = b_elems[0]["bldg_label"] or bid
                    errors.append(
                        f"{r['file']}: road {r['name']!r} intersects building {bname!r} footprint (area={area:.3f} m^2)"
                    )

        bids = sorted(building_groups)
        for i in range(len(bids)):
            for j in range(i + 1, len(bids)):
                b1, b2 = bids[i], bids[j]
                a1, a2 = bldg_aabbs[b1], bldg_aabbs[b2]
                if not _aabb_overlap(a1, a2, tol=0.05):
                    continue
                t1 = [t for e in building_groups[b1] for t in e["tris"]]
                t2 = [t for e in building_groups[b2] for t in e["tris"]]
                area = _footprint_overlap_area(t1, t2, _SHAP)
                if area > 0.05:
                    l1 = building_groups[b1][0]["bldg_label"] or b1
                    l2 = building_groups[b2][0]["bldg_label"] or b2
                    errors.append(f"building overlap between {l1!r} and {l2!r} (area={area:.3f} m^2)")

        # MEP touching/overlapping clashes, tray-to-busway (<300mm), tray-stack headroom, and clear height
        mep_flags = [_is_mep(e) for e in all_elems]
        stack_warns = 0
        if any(mep_flags):
            for i in range(len(all_elems)):
                if time.monotonic() - START_T > 30.0:
                    warnings.append("MEP clash scan stopped at 30s time budget")
                    break
                ei = all_elems[i]
                for j in range(i + 1, len(all_elems)):
                    if not (mep_flags[i] or mep_flags[j]):
                        continue
                    ej = all_elems[j]
                    pkey = tuple(sorted(((ei["file"], ei["gid"]), (ej["file"], ej["gid"]))))
                    if pkey not in reported_clash_pairs and not _is_floor_rest(ei, ej):
                        if mep_flags[i] and mep_flags[j]:
                            if _aabb_dist(ei["aabb"], ej["aabb"]) < 0.002 and _mesh_clearance(ei, ej) < 0.002:
                                _emit_mep_clash(
                                    ei,
                                    ej,
                                    f"MEP touching/overlapping clash (<2mm gap; leave 5-10mm gap at joints): "
                                    f"{ei['label']} ({ei['file']}) vs {ej['label']} ({ej['file']})",
                                )
                        elif _aabb_overlap(ei["aabb"], ej["aabb"], tol=0.0005) and _meshes_clash_gt_2mm(ei, ej, tol=0.0005):
                            _emit_mep_clash(
                                ei,
                                ej,
                                f"MEP solid clash (>0.5mm): {ei['label']} ({ei['file']}) vs {ej['label']} ({ej['file']})",
                            )
                    # Check tray <-> busway 300mm separation & tray-stack vertical headroom
                    ti = bool(TRAY_RE.search(ei["text"])) and not bool(BUSWAY_RE.search(ei["text"]))
                    bj = bool(BUSWAY_RE.search(ej["text"]))
                    tj = bool(TRAY_RE.search(ej["text"])) and not bool(BUSWAY_RE.search(ej["text"]))
                    bi = bool(BUSWAY_RE.search(ei["text"]))
                    if (ti and bj) or (tj and bi):
                        tray_e, bus_e = (ei, ej) if (ti and bj) else (ej, ei)
                        if _aabb_dist(tray_e["aabb"], bus_e["aabb"]) < 0.300:
                            clr = _mesh_clearance(tray_e, bus_e)
                            if clr < 0.299:
                                errors.append(
                                    f"tray-busway clearance {clr * 1000:.1f}mm < 300mm: {tray_e['label']} vs {bus_e['label']}"
                                )
                            elif clr < 0.300:
                                warnings.append(
                                    f"tray-busway borderline clearance {clr * 1000:.1f}mm (< 300mm): {tray_e['label']} vs {bus_e['label']}"
                                )
                    if stack_warns < 10 and ((ti and mep_flags[j]) or (tj and mep_flags[i])):
                        tray_e, other_e = (ei, ej) if ti else (ej, ei)
                        ta, oa = tray_e["aabb"], other_e["aabb"]
                        xy_dx = min(ta[3], oa[3]) - max(ta[0], oa[0])
                        xy_dy = min(ta[4], oa[4]) - max(ta[1], oa[1])
                        if xy_dx > 0.01 and xy_dy > 0.01 and (oa[2] > ta[5] or ta[2] > oa[5]):
                            vgap = max(0.0, oa[2] - ta[5], ta[2] - oa[5])
                            if 0.0 < vgap < 0.299:
                                stack_warns += 1
                                warnings.append(
                                    f"tray-stack vertical headroom {vgap * 1000:.1f}mm < 300mm "
                                    f"(check TIA-569-B access clearance): {tray_e['label']} vs {other_e['label']}"
                                )
            if clash_count > 15:
                errors.append(f"... plus {clash_count - 15} additional MEP solid clash(es)")

        # Overhead clear-height check (>= 2600mm AFF for suspended MEP / sprinkler items)
        clear_warns = 0
        for e in all_elems:
            if clear_warns >= 10:
                break
            if _is_mep(e) or SPRINKLER_RE.search(e["text"]):
                z_bot = e["aabb"][2]
                if 1.20 <= z_bot < 2.599:
                    clear_warns += 1
                    warnings.append(
                        f"overhead clear height {z_bot * 1000:.1f}mm < 2600mm "
                        f"(check TIA-942 clear-height rule): {e['label']} ({e['file']})"
                    )

print("__PLE_AUDIT_JSON__:" + json.dumps({"errors": errors[:30], "warnings": warnings[:30]}))
'''


def _build_programmatic_audit_script(task: str = "") -> str:
    """Return a self-contained Python 3 audit script for task deliverables."""
    _ = task
    return _PROGRAMMATIC_AUDIT_SCRIPT


async def run_programmatic_audit(
    session: taskbridge.Session,
    task: str = "",
) -> Dict[str, Any]:
    """Run deterministic CSV/XLSX/JSON/IFC checks in the task container before verification."""
    empty: Dict[str, Any] = {"errors": [], "warnings": [], "summary": ""}
    if getattr(session, "cancelled", None) and session.cancelled.is_set():
        return empty
    if not hasattr(session, "exec"):
        return empty
    script = _build_programmatic_audit_script(task)
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
            lines: List[str] = []
            if errors:
                lines.append("ERRORS (must be fixed; cannot PASS while non-empty):")
                lines.extend(f"- {e}" for e in errors[:20])
            if warnings:
                lines.append("WARNINGS (inspect carefully):")
                lines.extend(f"- {w}" for w in warnings[:15])
            return {
                "errors": errors,
                "warnings": warnings,
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

        work = conversation(
            WORKER,
            session,
            f"{task}\n\n## Plan\n{plan}",
            mcp_note=mcp_note,
        )
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
            max_turns=runtime_cfg.get("max_work_turns", 55),
        )

        max_fix_rounds = int(runtime_cfg.get("max_fix_rounds", 2))
        audit = await run_programmatic_audit(session, task) if max_fix_rounds > 0 else {
            "errors": [],
            "warnings": [],
            "summary": "",
        }
        if audit["errors"] or audit["warnings"]:
            await session.log(
                f"audit: errors={len(audit['errors'])} warnings={len(audit['warnings'])} "
                f"{'; '.join(audit['errors'][:5])}"
            )

        for _ in range(max_fix_rounds):
            if session.cancelled.is_set() or not _time_for(_until(CHECK_SHARE / 2)):
                break
            review = (
                f"## Task\n{task}\n\n## Plan\n{plan}\n\n## The engineer's summary\n{summary}"
            )
            if audit["summary"]:
                review += f"\n\n## Programmatic Pre-Check\n{audit['summary']}"
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
            audit_errors = list(audit.get("errors") or [])
            clean_pass = (
                bool(verdict.strip())
                and is_pass_verdict(verdict)
                and not verifier_truncated
                and not audit_errors
            )
            await session.log(
                f"verifier: truncated={verifier_truncated} audit_errors={len(audit_errors)} "
                f"{verdict[:1000]}"
            )
            if clean_pass or not _time_for(WRAP_UP_SEC):
                break
            if not verdict.strip() and not verifier_truncated and not audit_errors:
                break

            gap_sections: List[str] = []
            if audit_errors:
                gap_sections.append(
                    "Programmatic Pre-Check ERRORS (must fix):\n"
                    + "\n".join(f"- {err}" for err in audit_errors[:20])
                )
            if verifier_truncated and (not verdict.strip() or is_pass_verdict(verdict)):
                gap_sections.append(
                    "Verifier ran out of turns/time before completing all checks; do NOT assume "
                    "completion. Re-verify every hard constraint, register/ID alignment, file path, "
                    "and BIM/IFC geometry clearance directly on disk."
                )
            elif verdict.strip() and not is_pass_verdict(verdict):
                gap_sections.append(verdict)
            gaps_text = "\n\n".join(gap_sections)

            work.append(
                {
                    "role": "user",
                    "content": (
                        "A reviewer checked your files and found these gaps:\n"
                        f"{gaps_text}\n\n"
                        "Fix them immediately by writing or updating and regenerating "
                        "every required deliverable file on disk, re-run verification "
                        "checks on the updated files before your turn budget runs out, "
                        "then reply with your verified summary."
                    ),
                }
            )
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
            )
            summary = updated or summary
            audit = await run_programmatic_audit(session, task)
            if audit["errors"] or audit["warnings"]:
                await session.log(
                    f"audit(post-fix): errors={len(audit['errors'])} warnings={len(audit['warnings'])} "
                    f"{'; '.join(audit['errors'][:5])}"
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
