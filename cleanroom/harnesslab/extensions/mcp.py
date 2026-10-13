"""MCP client surface: config, naming, schema hygiene, output caps, in-process servers (WP5).

Implements spec 07 §2.5 (server config types; tool name
``mcp__<normalize(server)>__<tool>``, normalize = chars outside
``[A-Za-z0-9_-]`` -> ``_``), R16 (managed servers: http/sse + https only, no
``${VAR}`` expansion, no control chars), R18 + §4.3 (flatten top-level
``oneOf``/``anyOf``; drop only tools whose schema the API would reject; cap
descriptions at 2048 ``CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH``; strip
tool-invocation-like markup), R19 (connect backoff 500/1500/4000 ms; batches 3
local / 20 remote; non-blocking by default with an ``alwaysLoad`` blocking
group), R21-R22 + §4.4 (total and idle timeouts; auto-background after 120 s;
"outcome unknown, verify first" on interruption), R23 + §4.5 (25 000-token
output cap with 0.5x / 4x estimator bands, truncation footer, 65 536-char
``_meta`` strip), 03 R54 (MCP tools are deferred); takeaways 07#T5, 07#T6,
07#T7, 07#T10.

Scope: the *client stub* is real for in-process (``sdk``-type) servers backed by
Python callables (``InProcessMcpServer``) - enough to test naming, hygiene,
caps, permissions and deferral offline. stdio / http transports are declared
via the ``McpTransport`` Protocol and left for future work (documented gap).

Design choices
--------------
* The *tool* half of the name is normalised with the same rule as the server
  half, so every generated name is valid for the model API (a tool name that is
  already valid is unchanged).
* ``flatten_combinators`` merges the variants' properties (first definition of a
  property wins) and keeps only properties required by *every* variant.
* The "exact count" middle band of §4.5 uses an injectable ``count_tokens``
  (default: the same chars/4 + 1600-per-image estimate, since there is no
  token-count API offline). Images that do not fit are dropped with a note -
  there is no stdlib image downscaler (WORKPLAN G6).
* Idle timeouts need transport progress events, which the Protocol does not
  carry yet; ``tool_timeouts`` computes the values and ``McpTool`` enforces the
  total timeout and abort.

Deferred (documented gaps)
--------------------------
* stdio / http / sse / IDE transports, OAuth (R27), elicitation (R26), resources
  tools (R24), client capability negotiation (R25), first-turn hold policy and
  discovery cache (R20), parked-manifest tunnelling of SDK servers (R39, §4.7),
  ``ENABLE_MCP_LARGE_OUTPUT_FILES`` spilling. MCP permission-rule syntax (R17) is
  WP3's (``permissions.rules``).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol

from harnesslab.config import env_int, env_truthy
from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput
from harnesslab.types import ImageBlock, TextBlock

DESCRIPTION_CAP = 2048
DEFAULT_OUTPUT_TOKENS = 25_000
IMAGE_TOKENS_ESTIMATE = 1600
META_STRIP_CHARS = 65_536
CONNECT_BACKOFF_MS = (500, 1500, 4000)
LOCAL_BATCH, REMOTE_BATCH = 3, 20
AUTO_BACKGROUND_MS = 120_000
CHARS_PER_TOKEN = 4
MAX_TIMEOUT_MS = 2**31 - 1
DEFAULT_TOTAL_TIMEOUT_MS = 100_000_000
IDLE_STDIO_MS, IDLE_REMOTE_MS = 1_800_000, 300_000
LOCAL_TYPES = frozenset({"stdio", "sdk", "sse-ide", "ws-ide"})

UNKNOWN_OUTCOME_TEXT = (
    "The MCP tool call was interrupted before it returned. Its outcome is unknown: it may or may not "
    "have taken effect. Verify the current state before assuming it succeeded or running it again."
)


def truncation_footer(limit_tokens: int) -> str:
    return (
        f"[Output truncated: this MCP tool returned more than about {limit_tokens} tokens. If the tool "
        "supports pagination or filtering, call it again asking for a smaller slice. Otherwise tell the "
        "user the results may be incomplete.]"
    )


@dataclass
class McpServerConfig:
    name: str
    type: Literal["stdio", "sse", "http", "sse-ide", "ws-ide", "sdk", "claudeai-proxy"] = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: int | None = None
    always_load: bool = False
    source: str = "project"  # sdk | plugin | user | project | managed ...
    disable_auto_background: bool = False


@dataclass
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpTransport(Protocol):
    def list_tools(self) -> list[McpToolSpec]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Return MCP content blocks (``{"type": "text", "text": ...}`` / image)."""
        ...


class InProcessMcpServer:
    """``sdk``-type server: tools are Python callables returning MCP content blocks."""

    def __init__(self, name: str, tools: dict[str, tuple[McpToolSpec, Callable[[dict[str, Any]], list[dict[str, Any]]]]] | None = None) -> None:
        self.name = name
        self.tools = tools or {}

    def list_tools(self) -> list[McpToolSpec]:
        return [spec for spec, _ in self.tools.values()]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return self.tools[name][1](arguments)


# ------------------------------------------------------------------ naming (§2.5)

_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def normalize_server_name(name: str, *, hosted: bool = False) -> str:
    """Replace every char outside ``[A-Za-z0-9_-]`` with ``_``; hosted connectors
    also collapse runs of ``_``."""
    out = _INVALID.sub("_", name)
    if hosted:
        out = re.sub(r"_+", "_", out)
    return out


def mcp_tool_name(server: str, tool: str) -> str:
    return f"mcp__{normalize_server_name(server)}__{normalize_server_name(tool)}"


# ------------------------------------------------------------------ schema hygiene (§4.3)


def flatten_combinators(schema: dict[str, Any]) -> dict[str, Any]:
    """Turn a top-level ``oneOf``/``anyOf`` of object shapes into one object schema."""
    key = "oneOf" if "oneOf" in schema else "anyOf" if "anyOf" in schema else None
    if key is None or not isinstance(schema.get(key), list):
        return schema
    variants = [v for v in schema[key] if isinstance(v, dict)]
    props: dict[str, Any] = dict(schema.get("properties") or {})
    required_sets = []
    for v in variants:
        for pname, pschema in (v.get("properties") or {}).items():
            props.setdefault(pname, pschema)
        required_sets.append(set(v.get("required") or []))
    required = set.intersection(*required_sets) if required_sets else set()
    required |= set(schema.get("required") or [])
    out = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}
    out["type"] = "object"
    out["properties"] = props
    if required:
        out["required"] = sorted(required)
    else:
        out.pop("required", None)
    return out


def api_accepts(schema: dict[str, Any]) -> bool:
    """Would the model API accept this as a tool ``input_schema``?"""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if any(k in schema for k in ("oneOf", "anyOf", "allOf")):
        return False
    props = schema.get("properties", {})
    if not isinstance(props, dict) or not all(isinstance(v, dict) for v in props.values()):
        return False
    req = schema.get("required", [])
    if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
        return False
    try:
        json.dumps(schema)
    except (TypeError, ValueError):
        return False
    return True


_INVOKE_MARKUP = re.compile(r"</?\s*(?:[a-z]+:)?(?:function_calls|invoke|parameter|tool_use|tool_call|tool_result|function_results)\b[^>]*>", re.I)


def strip_invoke_markup(text: str) -> str:
    """Remove tags that look like tool-invocation markup from a description (R18)."""
    return _INVOKE_MARKUP.sub("", text)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_managed_config(cfg: McpServerConfig) -> list[str]:
    """Problems that make a server unacceptable from managed settings (R16); ``[]`` = OK."""
    problems: list[str] = []
    if cfg.type not in ("http", "sse"):
        problems.append(f"server {cfg.name!r}: managed servers must be http or sse, not {cfg.type}")
    if not cfg.url or not cfg.url.lower().startswith("https://"):
        problems.append(f"server {cfg.name!r}: managed servers need an https URL")
    strings = [cfg.name, cfg.url or "", cfg.command or "", *cfg.args, *cfg.env.keys(), *cfg.env.values(), *cfg.headers.keys(), *cfg.headers.values()]
    if any("${" in s for s in strings):
        problems.append(f"server {cfg.name!r}: ${{VAR}} expansion is not allowed in managed servers")
    if any(_CONTROL.search(s) for s in strings):
        problems.append(f"server {cfg.name!r}: control characters are not allowed")
    return problems


# ------------------------------------------------------------------ connection & timeouts (§4.4)


def connection_plan(servers: list[McpServerConfig], *, cached: set[str] | None = None) -> tuple[list[list[McpServerConfig]], list[list[McpServerConfig]]]:
    """``(blocking_batches, background_batches)``: ``alwaysLoad``/cached servers connect
    first; local servers in batches of 3, remote in batches of 20 (R19, 07#T6)."""
    cached = cached or set()

    def batches(group: list[McpServerConfig]) -> list[list[McpServerConfig]]:
        local = [s for s in group if s.type in LOCAL_TYPES]
        remote = [s for s in group if s.type not in LOCAL_TYPES]
        return [local[i : i + LOCAL_BATCH] for i in range(0, len(local), LOCAL_BATCH)] + [
            remote[i : i + REMOTE_BATCH] for i in range(0, len(remote), REMOTE_BATCH)
        ]

    blocking = [s for s in servers if s.always_load or s.name in cached]
    rest = [s for s in servers if s not in blocking]
    return batches(blocking), batches(rest)


def tool_timeouts(cfg: McpServerConfig, env: Mapping[str, str]) -> tuple[int, int | None]:
    """``(total_ms, idle_ms or None)`` per §4.4 (R21)."""
    total = cfg.timeout if cfg.timeout and cfg.timeout >= 1000 else env_int(env, "MCP_TOOL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT_MS)
    total = min(total, MAX_TIMEOUT_MS)
    if cfg.type in ("sse-ide", "ws-ide", "sdk"):
        return total, None
    idle_default = IDLE_STDIO_MS if cfg.type == "stdio" else IDLE_REMOTE_MS
    return total, env_int(env, "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", idle_default)


# ------------------------------------------------------------------ output cap (§4.5)


def _estimate(blocks: list[dict[str, Any]]) -> float:
    text = sum(len(b.get("text", "")) / CHARS_PER_TOKEN for b in blocks if b.get("type") == "text")
    images = sum(1 for b in blocks if b.get("type") == "image")
    return text + IMAGE_TOKENS_ESTIMATE * images


def _strip_meta(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for b in blocks:
        if b.get("type") == "text" and "_meta" in b and len(json.dumps(b["_meta"], default=str)) > META_STRIP_CHARS:
            b = {k: v for k, v in b.items() if k != "_meta"}
        out.append(b)
    return out


def _truncate(blocks: list[dict[str, Any]], limit: int, footer: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    chars = limit * CHARS_PER_TOKEN
    dropped_images = 0
    for b in blocks:
        if chars <= 0:
            break
        if b.get("type") == "text":
            take = b.get("text", "")[:chars]
            chars -= len(take)
            out.append({**b, "text": take})
        elif b.get("type") == "image":
            cost = IMAGE_TOKENS_ESTIMATE * CHARS_PER_TOKEN
            if chars >= cost:
                out.append(b)
                chars -= cost
            else:
                dropped_images += 1
        else:
            out.append(b)
    if dropped_images:
        out.append({"type": "text", "text": f"[{dropped_images} image(s) omitted to stay within the output limit]"})
    out.append({"type": "text", "text": footer})
    return out


def cap_output(
    blocks: list[dict[str, Any]],
    limit_tokens: int = DEFAULT_OUTPUT_TOKENS,
    *,
    count_tokens: Callable[[list[dict[str, Any]]], float] | None = None,
    footer: str | None = None,
) -> list[dict[str, Any]]:
    """Three-band cap (§4.5, 07#T5): cheap estimate under 0.5x passes; text-only over
    4x truncates without counting; otherwise count precisely and truncate if over."""
    blocks = _strip_meta(blocks)
    foot = footer or truncation_footer(limit_tokens)
    est = _estimate(blocks)
    if est <= 0.5 * limit_tokens:
        return blocks
    text_only = all(b.get("type") == "text" for b in blocks)
    if text_only and est > 4 * limit_tokens:
        return _truncate(blocks, limit_tokens, foot)
    if (count_tokens or _estimate)(blocks) <= limit_tokens:
        return blocks
    return _truncate(blocks, limit_tokens, foot)


# ------------------------------------------------------------------ tool wrapper


def _to_content(blocks: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for b in blocks:
        if b.get("type") == "image":
            out.append(ImageBlock(b.get("mimeType") or b.get("media_type") or "image/png", b.get("data", "")))
        elif b.get("type") == "text":
            out.append(TextBlock(str(b.get("text", ""))))
        else:
            out.append(TextBlock(json.dumps(b, default=str)))
    return out


class McpTool(Tool):
    """Wraps one MCP tool as a harness ``Tool`` (deferred by default)."""

    is_mcp = True
    should_defer = True

    def __init__(self, server: McpServerConfig, spec: McpToolSpec, transport: McpTransport, *, name: str | None = None, description: str | None = None, schema: dict[str, Any] | None = None, env: Mapping[str, str] | None = None) -> None:
        self.server = server
        self.spec = spec
        self.transport = transport
        self.env = dict(env or {})
        self.name = name or mcp_tool_name(server.name, spec.name)  # type: ignore[misc]
        self.input_schema = schema if schema is not None else spec.input_schema  # type: ignore[misc]
        self._description = description if description is not None else spec.description
        self.always_load = server.always_load  # type: ignore[misc]
        self.auto_background_ms = env_int(self.env, "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS", AUTO_BACKGROUND_MS)

    def description(self, ctx: Any = None) -> str:
        return self._description or self.name

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        return self.description()

    def _finish(self, blocks: list[dict[str, Any]]) -> ToolOutput:
        limit = env_int(self.env, "MAX_MCP_OUTPUT_TOKENS", DEFAULT_OUTPUT_TOKENS, lo=1)
        capped = cap_output(blocks, limit, footer=self.env.get("MCP_TRUNCATION_PROMPT_OVERRIDE") or None)
        return ToolOutput(data=capped, blocks=_to_content(capped))

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        """Run the call in a worker thread so abort / timeout / auto-background can act (§4.4)."""
        total_ms, _idle = tool_timeouts(self.server, self.env)
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["blocks"] = self.transport.call_tool(self.spec.name, dict(input))
            except Exception as e:  # noqa: BLE001 - surfaced as a tool error
                box["error"] = e
            done.set()

        threading.Thread(target=work, daemon=True, name=f"mcp-{self.name}").start()
        ctx = call_ctx.session
        can_background = not self.server.disable_auto_background and ctx.tasks is not None and ctx.notifications is not None
        waited_ms = 0.0
        step_ms = 20.0
        while not done.wait(step_ms / 1000):
            waited_ms += step_ms
            if call_ctx.abort.aborted:
                return ToolOutput(data=UNKNOWN_OUTCOME_TEXT, is_error=True)
            if can_background and waited_ms >= self.auto_background_ms:
                return self._background(done, box, call_ctx)
            if waited_ms >= total_ms:
                return ToolOutput(data=f"The MCP tool timed out after {int(total_ms)} ms. " + UNKNOWN_OUTCOME_TEXT, is_error=True)
        if "error" in box:
            return ToolOutput(data=f"MCP tool error: {box['error']}", is_error=True)
        return self._finish(list(box.get("blocks") or []))

    def _background(self, done: threading.Event, box: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        """06 R44: a long call becomes a background task that notifies on completion."""
        ctx = call_ctx.session
        task = ctx.tasks.register("mcp", f"{self.name} call")

        def wait_and_notify() -> None:
            done.wait()
            if ctx.tasks.get(task.id) is None or ctx.tasks.get(task.id).status != "running":
                return
            task.meta["notified"] = True
            if "error" in box:
                ctx.tasks.finish(task.id, "failed", error=str(box["error"]))
                ctx.notifications.notify(task.id, "failed", f"{self.name} failed", str(box["error"]))
                return
            out = self._finish(list(box.get("blocks") or []))
            text = "\n".join(b.text for b in (out.blocks or []) if isinstance(b, TextBlock))
            ctx.tasks.finish(task.id, "completed", result=out.data)
            ctx.notifications.notify(task.id, "completed", f"{self.name} finished", text)

        threading.Thread(target=wait_and_notify, daemon=True).start()
        return ToolOutput(
            data=f"This MCP call is slow and now continues as background task {task.id}. Its result will arrive "
            "as a notification; do not assume the outcome before then."
        )


def register_tools(server: McpServerConfig, transport: McpTransport, *, env: Mapping[str, str] | None = None) -> tuple[list[Tool], list[str]]:
    """``(tools, degraded_tool_names)`` (07 §4.3). A bad schema drops only that tool."""
    env = env or {}
    cap = env_int(env, "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH", DESCRIPTION_CAP, lo=1)
    no_prefix = server.type == "sdk" and env_truthy(env, "CLAUDE_AGENT_SDK_MCP_NO_PREFIX")
    tools: list[Tool] = []
    degraded: list[str] = []
    for spec in transport.list_tools():
        schema = flatten_combinators(spec.input_schema) if isinstance(spec.input_schema, dict) else spec.input_schema
        if not api_accepts(schema):
            degraded.append(spec.name)
            continue
        desc = strip_invoke_markup(spec.description or "")[:cap]
        name = spec.name if no_prefix else mcp_tool_name(server.name, spec.name)
        tools.append(McpTool(server, spec, transport, name=name, description=desc, schema=schema, env=env))
    return tools, degraded
