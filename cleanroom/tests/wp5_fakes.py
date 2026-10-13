"""Shared fakes for WP5 tests (no dependency on WP1-WP4 implementations)."""

from __future__ import annotations

import threading
from typing import Any, Iterator

from harnesslab.loop.state import QueryParams
from harnesslab.model.base import ModelRequest, StreamAccumulator
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput
from harnesslab.types import (
    ConversationItem,
    Message,
    MessageEvent,
    TerminalReason,
    ToolResultBlock,
    TurnEndEvent,
)


def mini_run_query(items: list[ConversationItem], ctx: SessionContext, params: QueryParams | None = None, deps: Any = None) -> Iterator[Any]:
    """A tiny stand-in for ``loop.query.run_query`` (WP1): stream from ``ctx.model``,
    run tool calls directly, repeat until a turn has no tool use or max turns."""
    params = params or QueryParams()
    msgs = [m for m in items if isinstance(m, Message)]
    turns = 0
    while True:
        turns += 1
        req = ModelRequest(model=ctx.model_id, messages=[m.to_api() for m in msgs], tools=[t.api_schema() for t in ctx.tools], query_source=params.query_source)
        acc = StreamAccumulator()
        for ev in ctx.model.stream(req, ctx.abort.signal):
            acc.feed(ev)
        assistant = acc.message()
        msgs.append(assistant)
        yield MessageEvent(assistant)
        uses = assistant.tool_uses()
        if not uses:
            yield TurnEndEvent(TerminalReason.COMPLETED, turns)
            return TerminalReason.COMPLETED
        by_name = {t.name: t for t in ctx.tools}
        results = []
        for u in uses:
            tool = by_name.get(u.name)
            if tool is None:
                results.append(ToolResultBlock(u.id, f"unknown tool {u.name}", True))
                continue
            out = tool.call(u.input, ToolCallContext(ctx, u.id, ctx.abort.signal))
            results.append(tool.map_result(out, u.id))
        user = Message("user", list(results))
        msgs.append(user)
        yield MessageEvent(user)
        if params.max_turns is not None and turns >= params.max_turns:
            yield TurnEndEvent(TerminalReason.MAX_TURNS, turns)
            return TerminalReason.MAX_TURNS


class RecordingRunner:
    """Wraps ``mini_run_query`` and records each call; optionally blocks until released."""

    def __init__(self, block: threading.Event | None = None) -> None:
        self.calls: list[tuple[list[ConversationItem], SessionContext, QueryParams]] = []
        self.block = block
        self.started = threading.Event()

    def __call__(self, items, ctx, params=None, deps=None):
        self.calls.append((list(items), ctx, params))
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        return mini_run_query(items, ctx, params, deps)


def no_deps(_ctx: SessionContext) -> None:
    return None


class NamedTool(Tool):
    """A do-nothing tool with a configurable name (for filtering tables)."""

    def __init__(self, name: str, *, aliases: tuple[str, ...] = (), result: str = "ok") -> None:
        self.name = name  # type: ignore[misc]
        self.aliases = aliases  # type: ignore[misc]
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        self.calls.append(input)
        return ToolOutput(data=self.result)


def names(tools: list[Tool]) -> list[str]:
    return [t.name for t in tools]
