"""Model protocol, request/stream types and the stream accumulator (FINAL - architect).

The agent loop talks to *any* model through the small ``Model`` protocol below:
``stream(request, abort) -> Iterator[StreamEvent]``. Two implementations ship:

* ``model.scripted.ScriptedModel`` - offline fake that replays scripted turns.
* ``model.anthropic_adapter.AnthropicModel`` - optional real backend (WP1).

Spec coverage
-------------
* 01 R9   tools are queued as soon as a tool_use block completes in the stream:
          ``StreamAccumulator.feed`` returns completed blocks one by one.
* 01 R14/R15  failures carry an ``ErrorKind`` + status + headers so the retry
          table can classify them (``APIError``).
* 01 R20/R26  overflow / prompt-too-long errors expose parsed token counts.
* 01 R36-R39  request fields: max_tokens, thinking, effort, speed, tool_choice.
* 01 R40/R41  ``SystemBlock.cache_control`` carries ``{type, ttl?, scope?}``.
* 01#T7  every request is tagged with ``query_source`` so policy can route on it.
* 01 R30  a tool_use whose JSON does not parse is kept with ``parse_error``.

Stream events deliberately mirror the Messages API event names so the
anthropic adapter is a thin mapping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Protocol, Union, runtime_checkable

from harnesslab.types import (
    ContentBlock,
    ErrorKind,
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from harnesslab.util.abort import AbortSignal

# ------------------------------------------------------------------ request


@dataclass
class SystemBlock:
    """One block of the system prompt as sent (02 §2.2). ``cache_control`` is
    ``None`` (uncached) or ``{"type": "ephemeral", "ttl"?: "5m"|"1h", "scope"?: "global"|"org"}``."""

    text: str
    cache_control: dict[str, Any] | None = None

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "text", "text": self.text}
        if self.cache_control:
            d["cache_control"] = dict(self.cache_control)
        return d


@dataclass
class ModelRequest:
    """Everything one model call needs. ``messages`` are already normalized and
    API-shaped (``loop.normalize`` output); ``tools`` are API tool schemas."""

    model: str
    system: list[SystemBlock] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 32_000
    thinking: dict[str, Any] | None = None  # {"type": "adaptive"} | {"type": "enabled", "budget_tokens": n} | {"type": "disabled"}
    effort: str | None = None  # low|medium|high|xhigh|max -> output_config.effort (01 R38)
    tool_choice: dict[str, Any] | None = None
    temperature: float | None = None
    stop_sequences: list[str] = field(default_factory=list)
    betas: list[str] = field(default_factory=list)
    speed: str | None = None  # "fast" (01 R39)
    query_source: str = "main"  # main|sdk|subagent|compact|hook|side_question|classifier|title|background...
    context_management: dict[str, Any] | None = None  # server context edits (05 R31/R32)
    stream: bool = True  # False for the non-streaming fallback (01 R14)
    metadata: dict[str, Any] = field(default_factory=dict)

    def replace(self, **changes: Any) -> "ModelRequest":
        from dataclasses import replace as _r

        return _r(self, **changes)


# ------------------------------------------------------------------ stream events


@dataclass
class MessageStart:
    message_id: str
    model: str
    usage: Usage = field(default_factory=Usage)
    type: ClassVar[str] = "message_start"


@dataclass
class ContentBlockStart:
    """Opens block ``index``. ``block`` is the initial (empty) block:
    ``TextBlock("")``, ``ThinkingBlock("")`` or ``ToolUseBlock(id, name)``."""

    index: int
    block: ContentBlock
    type: ClassVar[str] = "content_block_start"


@dataclass
class ContentBlockDelta:
    """``delta_type`` is one of ``text``, ``thinking``, ``signature``, ``input_json``."""

    index: int
    delta_type: str
    value: str
    type: ClassVar[str] = "content_block_delta"


@dataclass
class ContentBlockStop:
    index: int
    type: ClassVar[str] = "content_block_stop"


@dataclass
class MessageDelta:
    """Carries the stop reason and the *final* usage (01 R50: use the closing delta)."""

    stop_reason: StopReason | None
    usage: Usage = field(default_factory=Usage)
    type: ClassVar[str] = "message_delta"


@dataclass
class MessageStop:
    type: ClassVar[str] = "message_stop"


@dataclass
class Ping:
    type: ClassVar[str] = "ping"


StreamEvent = Union[MessageStart, ContentBlockStart, ContentBlockDelta, ContentBlockStop, MessageDelta, MessageStop, Ping]


# ------------------------------------------------------------------ errors


class APIError(Exception):
    """A failed model request, classified (01 R15, R19).

    Attributes mirror what the retry policy needs: HTTP ``status``, response
    ``headers`` (``retry-after``, ``x-should-retry``), and parsed token counts for
    overflow errors. ``is_unified_quota`` / ``is_hard_credit_limit`` refine 429s.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str = "",
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        actual_tokens: int | None = None,
        limit_tokens: int | None = None,
        is_unified_quota: bool = False,
        rejected_beta: str | None = None,
    ) -> None:
        super().__init__(message or kind.value)
        self.kind = kind
        self.message = message or kind.value
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.actual_tokens = actual_tokens
        self.limit_tokens = limit_tokens
        self.is_unified_quota = is_unified_quota
        self.rejected_beta = rejected_beta

    @property
    def retry_after_s(self) -> float | None:
        raw = self.headers.get("retry-after")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    def __repr__(self) -> str:
        return f"APIError({self.kind.value!r}, status={self.status!r}, {self.message!r})"


# ------------------------------------------------------------------ protocol


@runtime_checkable
class Model(Protocol):
    """Anything that can stream a model response."""

    name: str

    def stream(self, request: ModelRequest, abort: AbortSignal | None = None) -> Iterator[StreamEvent]:
        """Yield stream events; raise ``APIError`` on failure (possibly mid-stream)."""
        ...


# ------------------------------------------------------------------ accumulator


class StreamAccumulator:
    """Builds the assistant ``Message`` from stream events.

    ``feed(event)`` returns the list of blocks that *completed* on this event, so
    the loop can hand a finished ``ToolUseBlock`` to the executor immediately
    (01 R9). Tool-use JSON is parsed at block stop; on failure the block keeps
    ``raw_input`` and ``parse_error`` (01 R30, 03 R2).

    ``progress`` reports how far the stream got, for the mid-stream failure
    table (01 R15): ``nothing`` < ``started`` < ``thinking_only`` < ``real_output``.
    """

    def __init__(self) -> None:
        self.message_id: str | None = None
        self.model: str | None = None
        self.blocks: dict[int, ContentBlock] = {}
        self._json: dict[int, str] = {}
        self.completed: list[ContentBlock] = []
        self.stop_reason: StopReason | None = None
        self.usage = Usage()
        self.started = False
        self.finished = False

    @property
    def progress(self) -> str:
        if not self.started:
            return "nothing"
        real = any(
            (isinstance(b, TextBlock) and b.text) or isinstance(b, ToolUseBlock) for b in self.blocks.values()
        )
        if real:
            return "real_output"
        if any(isinstance(b, ThinkingBlock) and b.thinking for b in self.blocks.values()):
            return "thinking_only"
        return "started"

    def feed(self, ev: StreamEvent) -> list[ContentBlock]:
        done: list[ContentBlock] = []
        if isinstance(ev, MessageStart):
            self.started = True
            self.message_id, self.model, self.usage = ev.message_id, ev.model, ev.usage
        elif isinstance(ev, ContentBlockStart):
            self.started = True
            self.blocks[ev.index] = ev.block
            if isinstance(ev.block, ToolUseBlock):
                self._json[ev.index] = ""
        elif isinstance(ev, ContentBlockDelta):
            b = self.blocks.get(ev.index)
            if ev.delta_type == "text" and isinstance(b, TextBlock):
                b.text += ev.value
            elif ev.delta_type == "thinking" and isinstance(b, ThinkingBlock):
                b.thinking += ev.value
            elif ev.delta_type == "signature" and isinstance(b, ThinkingBlock):
                b.signature += ev.value
            elif ev.delta_type == "input_json":
                self._json[ev.index] = self._json.get(ev.index, "") + ev.value
        elif isinstance(ev, ContentBlockStop):
            b = self.blocks.get(ev.index)
            if isinstance(b, ToolUseBlock):
                raw = self._json.get(ev.index, "")
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                    if not isinstance(parsed, dict):
                        raise ValueError("tool input must be a JSON object")
                    b.input = parsed
                except ValueError as e:  # json.JSONDecodeError is a ValueError
                    b.input, b.raw_input, b.parse_error = {}, raw, str(e)
            if b is not None:
                self.completed.append(b)
                done.append(b)
        elif isinstance(ev, MessageDelta):
            self.stop_reason = ev.stop_reason
            self.usage = ev.usage
        elif isinstance(ev, MessageStop):
            self.finished = True
        return done

    def message(self) -> Message:
        """The assistant row so far (complete or partial), blocks in index order."""
        blocks = [self.blocks[i] for i in sorted(self.blocks)]
        return Message(
            "assistant",
            blocks,
            stop_reason=self.stop_reason,
            usage=self.usage,
            model=self.model,
            message_id=self.message_id,
        )
