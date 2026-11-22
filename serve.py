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
RESULT_CHARS = 20_000
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
    "file. Then reply with the plan: each deliverable with its exact path "
    "and format, what it must contain, the data and method to use, and how "
    "to check it."
)

WORKER = (
    "You are an expert engineer working on a task in a Linux container with "
    "no internet access. Your tools act inside it, and bash starts in "
    "{workdir}. A plan for the task follows it; use it, and correct it where "
    "the files show it is wrong. Compute with code, not by hand. Write each "
    "deliverable exactly where and in the format the task asks, then open it "
    "again to check it. When you are done, reply without calling a tool: a "
    "short summary of each deliverable, where it is, and the key results."
)

VERIFIER = (
    "You check an engineer's work on a task in a Linux container. You get the "
    "task, the plan, and the engineer's summary of what was delivered. Open "
    "every deliverable with your tools (bash starts in {workdir}) and check it "
    "against the task: the path, the format, every part the task asks for, "
    "and the key numbers, recomputed. Do not create or change any file. If "
    "everything holds, reply with the single word PASS and nothing else. "
    "Otherwise reply with a numbered list of the gaps, each specific enough "
    "to fix."
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
READ_ONLY: List[Dict[str, Any]] = TOOLS[:2]
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

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "effort": effort,
        "max_output_tokens": max_output_tokens,
        "max_fix_rounds": max_fix_rounds,
        "max_retries": max_retries,
        "max_failures": max_failures,
        "cache_mark": "claude" in model.lower(),
    }


def build_openai_client(runtime_cfg: Dict[str, Any]) -> openai.AsyncOpenAI:
    """Create a lazy AsyncOpenAI client from resolved runtime configuration."""
    return openai.AsyncOpenAI(
        base_url=runtime_cfg.get("base_url") or None,
        api_key=runtime_cfg.get("api_key") or "none",
        max_retries=5,
        timeout=1800,
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
        else:
            out[i] = {**m, "cache_control": {"type": "ephemeral"}}
    return out


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
                return (
                    f"{args['path']} is a binary file ({len(data)} bytes); "
                    "inspect it with bash"
                )
            return clip(data.decode("utf-8", "replace") or "(empty file)")

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
    """Truncate the older half of long tool results to recover from context overflow."""
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

    nudges, head, waits = 0, "", iter(RETRY_WAITS)
    while True:
        if session.cancelled.is_set():
            return session.final_message or head
        left = session.time_left()
        wrap_up = left is not None and left < until
        if wrap_up and messages[-1:] != [WRAP_UP]:
            messages.append(dict(WRAP_UP))

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
            content = await run_tool(session, fn, mcp_registry=mcp_registry)
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

        now_left = session.time_left()
        if now_left is not None and messages[-1].get("role") == "tool":
            messages[-1]["content"] += (
                f"\n[{max(0, int((now_left - until) // 60))} minutes left for this step]"
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
        )

        max_fix_rounds = int(runtime_cfg.get("max_fix_rounds", 2))
        for _ in range(max_fix_rounds):
            if session.cancelled.is_set() or not _time_for(_until(CHECK_SHARE / 2)):
                break
            review = (
                f"## Task\n{task}\n\n## Plan\n{plan}\n\n## The engineer's summary\n{summary}"
            )
            verdict = await agent(
                session,
                conversation(VERIFIER, session, review, mcp_note=mcp_note),
                verifier_tools,
                _until(CHECK_SHARE / 2),
                client=active_client,
                runtime_cfg=runtime_cfg,
                mcp_registry=mcp_registry,
                breaker=breaker,
            )
            await session.log(f"verifier: {verdict[:1000]}")
            if not verdict.strip() or is_pass_verdict(verdict) or not _time_for(WRAP_UP_SEC):
                break
            work.append(
                {
                    "role": "user",
                    "content": (
                        "A reviewer checked your files and found these gaps:\n"
                        f"{verdict}\n\nFix them, then reply with your updated summary."
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
            )
            summary = updated or summary

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
