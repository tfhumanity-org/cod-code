"""Core conversation types shared by every module (FINAL - owned by the architect).

This module is the vocabulary of the whole harness: content blocks, messages,
attachments, loop enums and the events the agent loop yields. It is fully
implemented (no ``NotImplementedError``) so that every work package can build on
it without waiting for anyone else.

Spec coverage
-------------
* 01 §2.2  TerminalReason / TransitionReason / SDK result subtypes.
* 01 §2.3  Yielded events (stream_request_start, rows, attachments, tombstones,
           system notices, retry status).
* 01 §2.4  API-error-as-message (``Message.is_api_error`` + ``api_error``).
* 01 §2.5  Meta messages (``Message.is_meta``).
* 01 R52   Reasons a synthetic tool result exists (``SyntheticReason``).
* 02 §2.3  Attachments are typed records rendered to text only at
           serialization time (``Attachment`` + ``ATTACHMENT_RENDERERS``).
* 03 §2.4  Tool result envelope (``ToolResultBlock``).
* 05 §2.3  Row fields that live on messages (uuid / parent_uuid / is_sidechain /
           is_compact_summary / source_tool_assistant_uuid).

Design note: blocks are plain dataclasses with a ``to_api()`` method returning the
Messages-API-shaped dict. ``block_from_api`` is the inverse. Keeping the API
shape at the edge (rather than passing dicts around) lets readers see exactly
which fields the harness relies on.
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Literal, Union

# --------------------------------------------------------------------------- ids


def new_uuid() -> str:
    """Return a fresh random UUID string (rows, messages, tool-use ids)."""
    return str(_uuid.uuid4())


def now_iso() -> str:
    """Current UTC time as ISO-8601 (transcript ``timestamp`` field, 05 §2.3)."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ----------------------------------------------------------------------- literals

Role = Literal["user", "assistant", "system"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"]


class ErrorKind(str, enum.Enum):
    """Classified cause of a failed model request (01 R15 / R19).

    The retry engine (``api.retry``) maps ``(ErrorKind, StreamProgress)`` to an
    action; the recovery ladder (``loop.recovery``) inspects the kind on a
    withheld API-error row.
    """

    OVERLOADED = "overloaded"            # 529
    RATE_LIMIT = "rate_limit"            # 429 (soft)
    CREDIT_LIMIT = "credit_limit"        # 429 hard credit / spend cap: never retry
    PROMPT_TOO_LONG = "prompt_too_long"  # input alone exceeds the window
    CONTEXT_OVERFLOW = "context_overflow"  # input + max_tokens > limit (self-heal, 01 R20)
    IMAGE_TOO_LARGE = "image_too_large"
    AUTH = "auth"                        # 401
    PERMISSION = "permission"            # 403
    NOT_FOUND = "not_found"              # 404 (also triggers non-streaming fallback)
    BAD_REQUEST = "bad_request"          # 400
    BETA_REJECTED = "beta_rejected"      # 400 naming a beta header (01 R25)
    SERVER_ERROR = "server_error"        # >= 500
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    STALLED = "stalled"                  # idle watchdog fired
    TRUNCATED = "truncated"              # connection dropped mid-output
    MALFORMED = "malformed"              # unparseable stream
    OUTPUT_FILTERED = "output_filtered"  # safety classifier stop (01 R32)
    REFUSAL = "refusal"
    BILLING = "billing"
    UNKNOWN = "unknown"


# ------------------------------------------------------------------ content blocks


@dataclass
class TextBlock:
    text: str
    cache_control: dict[str, Any] | None = None
    type: ClassVar[str] = "text"

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "text", "text": self.text}
        if self.cache_control:
            d["cache_control"] = dict(self.cache_control)
        return d


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""
    cache_control: dict[str, Any] | None = None
    type: ClassVar[str] = "thinking"

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "thinking", "thinking": self.thinking, "signature": self.signature}
        if self.cache_control:
            d["cache_control"] = dict(self.cache_control)
        return d


@dataclass
class ToolUseBlock:
    """A model-issued tool call.

    ``input`` is the parsed JSON object. When the streamed JSON did not parse,
    ``input`` is ``{}``, ``raw_input`` holds the bytes received and
    ``parse_error`` explains why (03 R2: the error result echoes the first bytes).
    """

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    raw_input: str | None = None
    parse_error: str | None = None
    cache_control: dict[str, Any] | None = None
    type: ClassVar[str] = "tool_use"

    def to_api(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}
        if self.cache_control:
            d["cache_control"] = dict(self.cache_control)
        return d


@dataclass
class ImageBlock:
    media_type: str
    data: str  # base64
    type: ClassVar[str] = "image"

    def to_api(self) -> dict[str, Any]:
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type, "data": self.data}}


@dataclass
class DocumentBlock:
    media_type: str
    data: str  # base64
    type: ClassVar[str] = "document"

    def to_api(self) -> dict[str, Any]:
        return {"type": "document", "source": {"type": "base64", "media_type": self.media_type, "data": self.data}}


@dataclass
class ToolReferenceBlock:
    """Result block of ToolSearch: the API expands it into the tool's schema (03 R56)."""

    tool_name: str
    type: ClassVar[str] = "tool_reference"

    def to_api(self) -> dict[str, Any]:
        return {"type": "tool_reference", "tool_name": self.tool_name}


@dataclass
class ToolResultBlock:
    """Answer to a ``ToolUseBlock`` (03 §2.4). Errors are wrapped in ``<tool_use_error>``
    by the tool pipeline, and carry ``is_error=True``."""

    tool_use_id: str
    content: str | list["ContentBlock"] = ""
    is_error: bool = False
    cache_control: dict[str, Any] | None = None
    type: ClassVar[str] = "tool_result"

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def to_api(self) -> dict[str, Any]:
        content: Any = self.content if isinstance(self.content, str) else [b.to_api() for b in self.content]
        d: dict[str, Any] = {"type": "tool_result", "tool_use_id": self.tool_use_id, "content": content}
        if self.is_error:
            d["is_error"] = True
        if self.cache_control:
            d["cache_control"] = dict(self.cache_control)
        return d


ContentBlock = Union[
    TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock, DocumentBlock, ToolReferenceBlock
]


def block_from_api(d: dict[str, Any]) -> ContentBlock:
    """Inverse of ``to_api`` (used by transcript loading and the anthropic adapter)."""
    t = d.get("type")
    cc = d.get("cache_control")
    if t == "text":
        return TextBlock(d.get("text", ""), cc)
    if t == "thinking":
        return ThinkingBlock(d.get("thinking", ""), d.get("signature", ""), cc)
    if t == "tool_use":
        return ToolUseBlock(d["id"], d["name"], dict(d.get("input") or {}), cache_control=cc)
    if t == "tool_result":
        c = d.get("content", "")
        content: Any = c if isinstance(c, str) else [block_from_api(x) for x in c]
        return ToolResultBlock(d["tool_use_id"], content, bool(d.get("is_error")), cc)
    if t == "image":
        s = d.get("source", {})
        return ImageBlock(s.get("media_type", ""), s.get("data", ""))
    if t == "document":
        s = d.get("source", {})
        return DocumentBlock(s.get("media_type", ""), s.get("data", ""))
    if t == "tool_reference":
        return ToolReferenceBlock(d.get("tool_name", ""))
    raise ValueError(f"unknown content block type: {t!r}")


# --------------------------------------------------------------------------- usage


@dataclass
class Usage:
    """Token usage of one API response (01 R47, R50).

    The 5m/1h cache-creation split MUST be preserved for cost (01 R50).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0
    web_search_requests: int = 0

    def context_total(self) -> int:
        """input + cache creation + cache read + output: the anchor of 01 R47 / 05 R1."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            *(getattr(self, f) + getattr(other, f) for f in self.__dataclass_fields__)  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


@dataclass
class ApiErrorDetail:
    """Detail carried by an API-error row (01 §2.4). Token fields are parsed from
    prompt-too-long / overflow errors (01 R20, R26)."""

    kind: ErrorKind
    message: str
    status: int | None = None
    actual_tokens: int | None = None
    limit_tokens: int | None = None


# ------------------------------------------------------------------------ messages


@dataclass
class Message:
    """One conversation row with role user / assistant / system.

    Flags mirror the transcript envelope (05 §2.3) so a ``Message`` can be
    persisted losslessly by ``transcript.rows``.
    """

    role: Role
    content: list[ContentBlock] = field(default_factory=list)
    uuid: str = field(default_factory=new_uuid)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=now_iso)
    # 01 §2.5 - synthetic nudges / reminders, hidden by UIs.
    is_meta: bool = False
    # 01 §2.4 - API failure represented as an assistant row.
    is_api_error: bool = False
    api_error: ApiErrorDetail | None = None
    # assistant-only fields
    stop_reason: StopReason | None = None
    usage: Usage | None = None
    model: str | None = None
    message_id: str | None = None  # API response id; groups rows into API rounds (05 §4.6)
    aborted_mid_stream: bool = False  # 01 R53
    # transcript / compaction flags (05 §2.3)
    is_compact_summary: bool = False
    is_sidechain: bool = False
    source_tool_assistant_uuid: str | None = None
    tool_use_result: Any = None  # structured tool output kept for UIs/hooks
    origin: str | None = None  # e.g. "task-notification", "user", "hook", "auto-continuation"
    subtype: str | None = None  # for system rows: "compact_boundary", "informational", ...
    extra: dict[str, Any] = field(default_factory=dict)  # e.g. compactMetadata on boundaries

    # ---- constructors
    @staticmethod
    def user(content: str | list[ContentBlock], *, is_meta: bool = False, origin: str | None = None) -> "Message":
        blocks: list[ContentBlock] = [TextBlock(content)] if isinstance(content, str) else list(content)
        return Message("user", blocks, is_meta=is_meta, origin=origin)

    @staticmethod
    def assistant(content: str | list[ContentBlock], **kw: Any) -> "Message":
        blocks: list[ContentBlock] = [TextBlock(content)] if isinstance(content, str) else list(content)
        return Message("assistant", blocks, **kw)

    @staticmethod
    def system(text: str, subtype: str = "informational", **kw: Any) -> "Message":
        return Message("system", [TextBlock(text)], subtype=subtype, **kw)

    # ---- queries
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.content if isinstance(b, ToolResultBlock)]

    def has_tool_use(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.content)

    def is_thinking_only(self) -> bool:
        """True when the assistant produced reasoning but nothing visible (01 R31)."""
        has_thinking = any(isinstance(b, ThinkingBlock) for b in self.content)
        visible = any(
            (isinstance(b, TextBlock) and b.text.strip()) or isinstance(b, ToolUseBlock) for b in self.content
        )
        return has_thinking and not visible

    def to_api(self) -> dict[str, Any]:
        """API-shaped ``{"role", "content"}`` (system rows are not sent this way)."""
        return {"role": self.role, "content": [b.to_api() for b in self.content]}


@dataclass
class Attachment:
    """Typed volatile context (02 §2.3), persisted as ``type: "attachment"`` rows and
    rendered as an ``isMeta`` user message wrapped in ``<system-reminder>`` only
    when the request is serialized (see ``render_attachment_text``)."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    uuid: str = field(default_factory=new_uuid)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=now_iso)


ConversationItem = Union[Message, Attachment]

#: Registry of attachment renderers: ``type -> fn(payload) -> text | None``.
#: ``context.attachments`` (WP4) registers the real renderers at import time; any
#: module may register more (e.g. agents register ``task_notification``).
ATTACHMENT_RENDERERS: dict[str, Callable[[dict[str, Any]], str | None]] = {}


def render_attachment_text(att: Attachment) -> str | None:
    """Render an attachment to reminder text. ``None`` means "send nothing".

    Falls back to a generic ``type: json`` rendering so unknown attachment types are
    still visible (and obviously so) rather than silently dropped.
    """
    fn = ATTACHMENT_RENDERERS.get(att.type)
    if fn is not None:
        return fn(att.payload)
    return f"[{att.type}] {json.dumps(att.payload, sort_keys=True, default=str)}"


# ---------------------------------------------------------------------- loop enums


class TerminalReason(str, enum.Enum):
    """Why a turn ended (01 §2.2)."""

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    ABORTED_STREAMING = "aborted_streaming"
    ABORTED_TOOLS = "aborted_tools"
    BLOCKING_LIMIT = "blocking_limit"
    RAPID_REFILL_BREAKER = "rapid_refill_breaker"
    PROMPT_TOO_LONG = "prompt_too_long"
    IMAGE_ERROR = "image_error"
    MODEL_ERROR = "model_error"
    API_ERROR = "api_error"
    MALFORMED_TOOL_USE_EXHAUSTED = "malformed_tool_use_exhausted"
    STOP_HOOK_PREVENTED = "stop_hook_prevented"
    HOOK_STOPPED = "hook_stopped"
    TOOL_DEFERRED = "tool_deferred"
    BACKGROUND_REQUESTED = "background_requested"
    BUDGET_EXCEEDED = "budget_exceeded"  # design choice: makes 01 R8 observable in-loop


class TransitionReason(str, enum.Enum):
    """Why the loop iterates again (01 §2.2)."""

    NEXT_TURN = "next_turn"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    TRUNCATED_RESPONSE_RECOVERY = "truncated_response_recovery"
    MALFORMED_TOOL_USE_RETRY = "malformed_tool_use_retry"
    THINKING_ONLY_RETRY = "thinking_only_retry"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    PRECOMPUTED_COMPACT_SWAP = "precomputed_compact_swap"
    REFUSAL_RETRY = "refusal_retry"
    STOP_HOOK_BLOCKING = "stop_hook_blocking"


class SdkResultSubtype(str, enum.Enum):
    """``result`` frame subtype for hosts (01 §2.2, 07 §2.6)."""

    SUCCESS = "success"
    ERROR_DURING_EXECUTION = "error_during_execution"
    ERROR_MAX_TURNS = "error_max_turns"
    ERROR_MAX_BUDGET_USD = "error_max_budget_usd"
    ERROR_MAX_STRUCTURED_OUTPUT_RETRIES = "error_max_structured_output_retries"


class SyntheticReason(str, enum.Enum):
    """Why a tool_use has a synthetic (not real) result (01 R52, 03 R13).
    Every synthetic result MUST say why, so the model neither repeats side effects
    nor refuses falsely (01#T5)."""

    NEWER_MESSAGE = "newer_message"          # turn ended to deliver a newer user message
    SESSION_ENDED = "session_ended"          # outcome unknown; verify before rerunning
    SIBLING_DENIED = "sibling_denied"        # skipped because a sibling call was denied
    USER_DECLINED = "user_declined"          # stop
    INTERRUPTED = "interrupted"              # user pressed interrupt
    STREAMING_FALLBACK = "streaming_fallback"  # stream retried / model fell back (03#T2)
    MISSING = "missing"                      # pairing repair found no result (01 R56)


#: Own wording for each synthetic reason (01 R52 requires the *content*: why there
#: is no real result and what the model should do). Shared by tools.executor (WP2)
#: and loop.normalize (WP1), hence defined here.
SYNTHETIC_RESULT_TEXT: dict[SyntheticReason, str] = {
    SyntheticReason.NEWER_MESSAGE: (
        "Not run: the turn ended early so a newer user message could be delivered."
    ),
    SyntheticReason.SESSION_ENDED: (
        "No result was recorded because the session ended while this call was running. "
        "The outcome is unknown: verify the current state before running it again."
    ),
    SyntheticReason.SIBLING_DENIED: (
        "Skipped: another tool call in the same batch was denied, so this one was not run."
    ),
    SyntheticReason.USER_DECLINED: (
        "The user declined this action. It did not run. Stop and wait for the user's direction."
    ),
    SyntheticReason.INTERRUPTED: (
        "Interrupted by the user before a result was produced. The action may be incomplete."
    ),
    SyntheticReason.STREAMING_FALLBACK: (
        "Discarded: the response was retried, so this call from the abandoned attempt never ran."
    ),
    SyntheticReason.MISSING: (
        "Tool result missing due to an internal error. The outcome is unknown: verify before retrying."
    ),
}


# -------------------------------------------------------------------- loop events


@dataclass
class StreamRequestStart:
    """Emitted right before each model call (01 R2 step 5)."""

    model: str
    attempt: int = 1
    kind: ClassVar[str] = "stream_request_start"


@dataclass
class RawStreamEvent:
    """A raw model stream event, forwarded for partial-message UIs."""

    event: Any
    kind: ClassVar[str] = "stream_event"


@dataclass
class MessageEvent:
    """A completed assistant row or a user/tool-result row."""

    message: Message
    kind: ClassVar[str] = "message"


@dataclass
class AttachmentEvent:
    attachment: Attachment
    kind: ClassVar[str] = "attachment"


@dataclass
class TombstoneEvent:
    """Retracts a previously emitted partial row (01 R16, R34)."""

    uuid: str
    kind: ClassVar[str] = "tombstone"


@dataclass
class SystemNoticeEvent:
    """Model switch, compact boundary, hook warnings, ... (01 §2.3, 07 §2.6 system/*)."""

    subtype: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    kind: ClassVar[str] = "system"


@dataclass
class RetryStatusEvent:
    """Countdown info for UIs (01 R24)."""

    error: str
    delay_ms: float
    attempt: int
    max_attempts: int
    kind: ClassVar[str] = "api_retry"


@dataclass
class ToolProgressEvent:
    tool_use_id: str
    data: Any = None
    kind: ClassVar[str] = "tool_progress"


@dataclass
class TurnEndEvent:
    """Final event of ``run_query``: the terminal reason, logged per turn (01 R1)."""

    reason: TerminalReason
    turn_count: int
    kind: ClassVar[str] = "turn_end"


LoopEvent = Union[
    StreamRequestStart,
    RawStreamEvent,
    MessageEvent,
    AttachmentEvent,
    TombstoneEvent,
    SystemNoticeEvent,
    RetryStatusEvent,
    ToolProgressEvent,
    TurnEndEvent,
]
