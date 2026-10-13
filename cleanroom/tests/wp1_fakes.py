"""Fakes for WP1 tests: a synchronous tool executor, a hook runner, a compactor.

They implement the ``interfaces`` Protocols just enough for the loop, so WP1's
tests pass while WP2-WP4 are still stubs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from harnesslab.api.request import build_model_request
from harnesslab.api.retry import call_with_retries
from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.interfaces import CompactResult
from harnesslab.loop.query import run_query
from harnesslab.loop.state import QueryDeps, QueryParams
from harnesslab.types import (
    SYNTHETIC_RESULT_TEXT,
    Message,
    MessageEvent,
    SyntheticReason,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass
class _Outcome:
    end_turn: str | None = None


@dataclass
class _Call:
    block: ToolUseBlock
    result: Message | None = None
    emitted: bool = False
    outcome: _Outcome = field(default_factory=_Outcome)


class FakeExecutor:
    """Runs each tool synchronously when added (or lazily when ``lazy``).

    ``handlers`` maps tool name -> fn(input) -> str (raise to make an error).
    ``on_add`` is called with every block (e.g. to trigger an abort).
    """

    def __init__(self, handlers: dict[str, Callable[[dict], str]] | None = None, *, lazy: bool = False,
                 on_add: Callable[[ToolUseBlock], None] | None = None, end_turn: str | None = None) -> None:
        self.handlers = handlers or {}
        self.lazy = lazy
        self.on_add = on_add
        self.end_turn = end_turn
        self.calls: list[_Call] = []
        self.discarded: list[SyntheticReason] = []
        self.ran: list[str] = []

    def _run(self, c: _Call) -> None:
        fn = self.handlers.get(c.block.name)
        if fn is None:
            c.result = Message.user([ToolResultBlock(c.block.id, f"<tool_use_error>No such tool: {c.block.name}</tool_use_error>", True)])
        else:
            self.ran.append(c.block.id)
            try:
                c.result = Message.user([ToolResultBlock(c.block.id, fn(c.block.input))])
            except Exception as e:  # noqa: BLE001
                c.result = Message.user([ToolResultBlock(c.block.id, str(e), True)])
        c.result.source_tool_assistant_uuid = "x"
        c.outcome.end_turn = self.end_turn

    def add(self, block: ToolUseBlock) -> None:
        c = _Call(block)
        self.calls.append(c)
        if self.on_add:
            self.on_add(block)
        if not self.lazy:
            self._run(c)

    def drain_completed(self):
        for c in self.calls:
            if c.result is None:
                break
            if not c.emitted:
                c.emitted = True
                yield MessageEvent(c.result)

    def drain_all(self):
        for c in self.calls:
            if c.result is None:
                self._run(c)
        yield from self.drain_completed()

    def discard(self, reason: SyntheticReason) -> list[Message]:
        self.discarded.append(reason)
        out = []
        for c in self.calls:
            if c.result is None:
                c.result = Message.user([ToolResultBlock(c.block.id, SYNTHETIC_RESULT_TEXT[reason], True)])
                c.emitted = True
                out.append(c.result)
        return out

    def results(self) -> list[Message]:
        return [c.result for c in self.calls if c.result is not None]

    def context_layers(self):
        return []

    @property
    def has_pending(self) -> bool:
        return any(c.result is None for c in self.calls)


class FakeHooks:
    """``HookRunner`` returning scripted results per event."""

    def __init__(self, responses: dict[HookEvent, Callable[[dict], AggregatedHookResult]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[HookEvent, dict]] = []

    def run(self, event, payload, ctx):
        self.calls.append((event, payload))
        fn = self.responses.get(event)
        return fn(payload) if fn else AggregatedHookResult()


class StubCompactor:
    """``Compactor`` with scripted reactive behaviour."""

    def __init__(self, *, reactive_ok: bool = True, thrash: bool = False) -> None:
        self.reactive_ok = reactive_ok
        self.thrash = thrash
        self.reactive_calls: list[int | None] = []

    def maybe_autocompact(self, items, ctx):
        return None

    def reactive(self, items, ctx, token_gap):
        self.reactive_calls.append(token_gap)
        if not self.reactive_ok:
            return None
        keep = [it for it in items if isinstance(it, Message) and it.role == "user" and not it.is_meta][-1:]
        summary = Message.user("Summary of the earlier conversation.", is_meta=True)
        return CompactResult(items=[summary, *keep], trigger="reactive")

    def thrashing(self, ctx):
        return self.thrash


def make_deps(executor_factory: Callable[[], Any] | None = None, **kw: Any) -> QueryDeps:
    """Real WP1 model call + request builder; fake executor by default."""
    executors: list[Any] = []

    def make_executor(ctx, assistant_uuid):
        ex = executor_factory() if executor_factory else FakeExecutor()
        executors.append(ex)
        return ex

    deps = QueryDeps(call_model=call_with_retries, build_request=build_model_request, make_executor=make_executor, **kw)
    deps.executors = executors  # type: ignore[attr-defined]
    return deps


def no_sleep(ctx) -> list[float]:
    """Record retry sleeps instead of sleeping."""
    slept: list[float] = []
    ctx.extra["retry_sleep"] = lambda s: slept.append(s) or False
    return slept


def drive(items, ctx, params: QueryParams | None = None, deps: QueryDeps | None = None):
    """Run ``run_query`` to completion: ``(events, terminal_reason)``."""
    gen = run_query(items, ctx, params or QueryParams(), deps or make_deps())
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def messages(events) -> list[Message]:
    return [e.message for e in events if isinstance(e, MessageEvent)]
