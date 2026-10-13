"""ScriptedModel - an offline fake LLM that replays scripted assistant turns (FINAL - architect).

This is what makes the whole harness testable without a network: the loop,
streaming tool dispatch, permissions, retries and compaction all run against a
script of turns. Each call to ``stream`` consumes the next ``ScriptedTurn`` and
turns it into realistic stream events (text arrives in small deltas, tool input
JSON arrives in fragments, usage arrives on the closing ``MessageDelta``).

Supported scenarios (and the spec behaviour each one exercises):

* plain text / tool_use turns ............................. 01 R2, R9, R10
* ``error=APIError(...)`` raised before any event .......... 01 R17-R21 retries
* ``error_after_events=n`` raised mid-stream ............... 01 R15, R16 tombstones
* ``stop_reason="max_tokens"`` ............................. 01 R28 continuation
* ``raw_tool_json={id: "{bad"}`` malformed tool input ...... 01 R30
* thinking-only turns (``ThinkingBlock`` only) ............. 01 R31
* a callable turn ``fn(request) -> ScriptedTurn`` ........... dynamic replies
* ``check=fn(request)`` assertions on what the loop sent .... cache/normalize tests

``ScriptedModel.requests`` records every ``ModelRequest`` for inspection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Union

from harnesslab.model.base import (
    APIError,
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    ModelRequest,
    StreamEvent,
)
from harnesslab.types import (
    ContentBlock,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
    new_uuid,
)
from harnesslab.util.abort import AbortedError, AbortSignal


class ScriptExhausted(AssertionError):
    """The loop asked for more model turns than the script provides."""


@dataclass
class ScriptedTurn:
    blocks: list[ContentBlock] = field(default_factory=list)
    stop_reason: StopReason | None = None  # default: tool_use if any tool block else end_turn
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=100, output_tokens=20))
    error: APIError | None = None
    error_after_events: int | None = None  # None: raise before streaming anything
    raw_tool_json: dict[str, str] = field(default_factory=dict)  # tool id -> raw (maybe invalid) JSON
    check: Callable[[ModelRequest], None] | None = None
    chunk_size: int = 16


TurnSpec = Union[ScriptedTurn, Callable[[ModelRequest], ScriptedTurn]]


# ------------------------------------------------------------------ helpers


def say(text: str, **kw: Any) -> ScriptedTurn:
    """A turn that only says ``text``."""
    return ScriptedTurn([TextBlock(text)], **kw)


def call(name: str, input: dict[str, Any] | None = None, *, id: str | None = None) -> ToolUseBlock:
    """A tool_use block (use inside ``turn(...)``)."""
    return ToolUseBlock(id or f"toolu_{new_uuid()[:12]}", name, dict(input or {}))


def turn(*blocks: ContentBlock | str, **kw: Any) -> ScriptedTurn:
    """``turn("I'll read it", call("Read", {...}))`` - strings become text blocks."""
    bl: list[ContentBlock] = [TextBlock(b) if isinstance(b, str) else b for b in blocks]
    return ScriptedTurn(bl, **kw)


def fail(error: APIError, *, after_events: int | None = None) -> ScriptedTurn:
    """A turn that raises ``error`` (optionally after ``after_events`` events)."""
    return ScriptedTurn(error=error, error_after_events=after_events)


# ------------------------------------------------------------------ model


class ScriptedModel:
    """Replays ``turns`` in order. See module docstring."""

    def __init__(self, turns: Iterable[TurnSpec] = (), name: str = "scripted-model") -> None:
        self.name = name
        self._turns: list[TurnSpec] = list(turns)
        self.requests: list[ModelRequest] = []

    def add(self, *turns: TurnSpec) -> "ScriptedModel":
        self._turns.extend(turns)
        return self

    @property
    def remaining(self) -> int:
        return len(self._turns)

    def stream(self, request: ModelRequest, abort: AbortSignal | None = None) -> Iterator[StreamEvent]:
        self.requests.append(request)
        if not self._turns:
            raise ScriptExhausted(f"no scripted turn left for request #{len(self.requests)}")
        spec = self._turns.pop(0)
        t = spec(request) if callable(spec) else spec
        if t.check is not None:
            t.check(request)
        return self._events(t, request, abort)

    # ---- event generation
    def _events(self, t: ScriptedTurn, request: ModelRequest, abort: AbortSignal | None) -> Iterator[StreamEvent]:
        if t.error is not None and t.error_after_events is None:
            raise t.error
        emitted = 0

        def gate(ev: StreamEvent) -> StreamEvent:
            nonlocal emitted
            if abort is not None and abort.aborted:
                raise AbortedError(abort.reason or "aborted")
            if t.error is not None and t.error_after_events is not None and emitted >= t.error_after_events:
                raise t.error
            emitted += 1
            return ev

        start_usage = Usage(input_tokens=t.usage.input_tokens)
        yield gate(MessageStart(f"msg_{new_uuid()[:12]}", request.model or self.name, start_usage))
        n = t.chunk_size
        for i, b in enumerate(t.blocks):
            if isinstance(b, TextBlock):
                yield gate(ContentBlockStart(i, TextBlock("")))
                for j in range(0, max(len(b.text), 1), n):
                    if b.text:
                        yield gate(ContentBlockDelta(i, "text", b.text[j : j + n]))
            elif isinstance(b, ThinkingBlock):
                yield gate(ContentBlockStart(i, ThinkingBlock("")))
                for j in range(0, len(b.thinking), n):
                    yield gate(ContentBlockDelta(i, "thinking", b.thinking[j : j + n]))
                if b.signature:
                    yield gate(ContentBlockDelta(i, "signature", b.signature))
            elif isinstance(b, ToolUseBlock):
                yield gate(ContentBlockStart(i, ToolUseBlock(b.id, b.name)))
                raw = t.raw_tool_json.get(b.id, json.dumps(b.input))
                for j in range(0, len(raw), n):
                    yield gate(ContentBlockDelta(i, "input_json", raw[j : j + n]))
            else:
                raise TypeError(f"ScriptedModel cannot stream block {type(b).__name__}")
            yield gate(ContentBlockStop(i))
        stop = t.stop_reason or ("tool_use" if any(isinstance(b, ToolUseBlock) for b in t.blocks) else "end_turn")
        yield gate(MessageDelta(stop, t.usage))
        yield gate(MessageStop())
        if t.error is not None:  # error_after_events beyond the end: raise at the very end
            raise t.error
