"""Tool registry: lookup by name/alias, enablement, deferral, API schemas (WP2).

Implements spec 03 R1 (resolve by name or alias; unknown -> immediate error;
wait for still-connecting MCP servers), 03 R54-R55 (a tool is deferred if MCP,
``should_defer`` or remotely listed, ``always_load`` cancels; never-deferred
set; deferred tools announced by name via a delta attachment), 03 §2.2
simple mode (``CLAUDE_CODE_SIMPLE`` -> Bash, Read, Edit), 03#T8 / P15.

Design choices (spec silent)
----------------------------
* MCP tool names follow ``mcp__<server>__<tool>``; a server is "still
  connecting" when its name is in ``ctx.extra["mcp_pending_servers"]`` (a set,
  or a zero-argument callable returning one) - maintained by WP5.
* The "remote list" of deferred tools comes from
  ``ctx.extra["remote_deferred_tools"]`` and the "configurable list of
  never-deferred built-ins" from ``ctx.extra["never_deferred_tools"]``.
* Deferral of a tool that is not concurrency-relevant never changes its
  behaviour, only whether its schema is in the request.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping

from harnesslab.config import env_truthy
from harnesslab.tools.base import Tool

NEVER_DEFERRED = frozenset({"ToolSearch", "StructuredOutput"})
SIMPLE_MODE_TOOLS = ("Bash", "Read", "Edit")
MCP_WAIT_S = 5.0
MCP_POLL_S = 0.05


def mcp_server_of(name: str) -> str | None:
    """``mcp__github__create_issue`` -> ``github``; other names -> None."""
    if not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__") :]
    server, sep, _ = rest.partition("__")
    return server if sep else None


def pending_mcp_servers(extra: Mapping[str, Any]) -> set[str]:
    p = extra.get("mcp_pending_servers")
    if callable(p):
        p = p()
    return set(p or ())


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = (), *, never_deferred: Iterable[str] = ()) -> None:
        self._tools: list[Tool] = []
        self.never_deferred = NEVER_DEFERRED | frozenset(never_deferred)
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> None:
        """Add a tool; a tool with the same name replaces the earlier one."""
        self._tools = [t for t in self._tools if t.name != tool.name]
        self._tools.append(tool)

    def get(self, name: str) -> Tool | None:
        """Exact name first, then aliases (03 R1)."""
        for t in self._tools:
            if t.name == name:
                return t
        for t in self._tools:
            if name in t.aliases:
                return t
        return None

    def resolve(
        self,
        name: str,
        *,
        pending_servers: Callable[[], set[str]] | None = None,
        refresh: Callable[[], Iterable[Tool]] | None = None,
        timeout_s: float = MCP_WAIT_S,
        poll_s: float = MCP_POLL_S,
    ) -> Tool | None:
        """``get`` that waits (R1) while the tool's MCP server is still connecting.

        ``refresh`` returns the current tool list (MCP tools appear as servers
        connect); ``pending_servers`` the names of servers still connecting.
        """
        tool = self.get(name)
        server = mcp_server_of(name)
        if tool is not None or server is None or pending_servers is None:
            return tool
        deadline = time.monotonic() + timeout_s
        while server in pending_servers() and time.monotonic() < deadline:
            time.sleep(poll_s)
            if refresh is not None:
                for t in refresh():
                    if self.get(t.name) is None:
                        self.add(t)
            tool = self.get(name)
            if tool is not None:
                return tool
        if refresh is not None:
            for t in refresh():
                if self.get(t.name) is None:
                    self.add(t)
        return self.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools)

    def enabled(self, env: Mapping[str, str] | None = None) -> list[Tool]:
        """Enabled tools, reduced to the simple-mode pool when ``CLAUDE_CODE_SIMPLE``."""
        tools = []
        for t in self._tools:
            try:
                if t.is_enabled():
                    tools.append(t)
            except Exception:  # a broken predicate disables only that tool
                continue
        if env is not None and env_truthy(env, "CLAUDE_CODE_SIMPLE"):
            tools = [t for t in tools if t.name in SIMPLE_MODE_TOOLS]
        return tools

    def is_deferred(self, tool: Tool, *, remote_deferred: frozenset[str] = frozenset()) -> bool:
        """R54: MCP / ``should_defer`` / remote list, unless never-deferred or ``always_load``."""
        return is_deferred_tool(tool, remote_deferred=remote_deferred, never_deferred=self.never_deferred)

    def api_schemas(
        self,
        *,
        model_id: str,
        lean: bool,
        discovered: set[str],
        remote_deferred: frozenset[str] = frozenset(),
        env: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Schemas for loaded tools: non-deferred + discovered deferred (03 R54)."""
        return [
            t.api_schema(model_id, lean)
            for t in self.enabled(env)
            if not self.is_deferred(t, remote_deferred=remote_deferred) or t.name in discovered
        ]

    def deferred_names(
        self, discovered: set[str], *, remote_deferred: frozenset[str] = frozenset()
    ) -> list[str]:
        """Names announced in the deferred-tools attachment (03 R55): deferred and
        not yet discovered, sorted for a stable (cache-friendly) announcement."""
        return sorted(
            t.name
            for t in self.enabled()
            if self.is_deferred(t, remote_deferred=remote_deferred) and t.name not in discovered
        )


def is_deferred_tool(
    tool: Tool, *, remote_deferred: frozenset[str] = frozenset(), never_deferred: frozenset[str] = NEVER_DEFERRED
) -> bool:
    if tool.name in never_deferred or tool.always_load:
        return False
    return bool(tool.is_mcp or tool.should_defer or tool.name in remote_deferred)


def registry_for(ctx: Any) -> ToolRegistry:
    """Registry over ``ctx.tools`` honouring the ctx-level deferral knobs."""
    return ToolRegistry(ctx.tools, never_deferred=ctx.extra.get("never_deferred_tools") or ())


def remote_deferred_for(ctx: Any) -> frozenset[str]:
    return frozenset(ctx.extra.get("remote_deferred_tools") or ())
