"""Tool declaration contract (FINAL - architect).

Every tool - built-in, MCP-wrapped, agent, skill, host-provided - subclasses
``Tool``. The class attributes and methods map 1:1 onto the ``ToolDef`` record
of spec 03 §2.1, with the same conservative defaults: enabled, **not** read-only,
**not** concurrency-safe, not destructive (03 §2.1: "a tool must opt in to
parallelism").

Spec coverage
-------------
* 03 §2.1  ToolDef fields and defaults.
* 03 §2.2  ``backgrounding`` / ``should_defer`` / result size budgets.
* 03 R3    ``coerce_input`` returns notes describing what was rewritten.
* 03 R5    ``validate_input`` may return ``behavior="ask"`` and an error code.
* 03 R12   ``ContextLayer`` - context mutations (allowed tools, model, effort,
           cwd) a tool asks for; applied immediately for serial tools and after
           the parallel run for safe ones.
* 03 §2.4  ``ToolOutput`` -> ``ToolResultBlock`` via ``map_result``.
* 03 #T11  ``ToolErrorCode`` - numeric codes so recovery effectiveness can be
           measured.
* 04 §4.5  ``classifier_projection`` - compact text for the safety judge.

Per-call state is passed in ``ToolCallContext`` (03 §2.1 ``create(call_ctx)``):
abort signal, tool-use id, siblings in the same turn, progress callback.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal

from harnesslab.permissions.types import Decision
from harnesslab.types import ContentBlock, ConversationItem, TextBlock, ToolResultBlock, ToolUseBlock
from harnesslab.util.abort import AbortSignal

if TYPE_CHECKING:
    from harnesslab.runtime import SessionContext

UNBOUNDED = 10**12  # "INF" result budget (Read self-limits by tokens, 03 §2.2)


class ToolErrorCode(enum.IntEnum):
    """Numeric error codes attached to validation failures (03 R5, 03#T11).

    Values are this implementation's own; only 9 (multiple matches) is fixed by
    spec 03 R31.
    """

    NONE = 0
    INPUT_VALIDATION = 1
    FILE_NOT_FOUND = 2
    NOT_READ_FIRST = 3
    STALE_FILE = 4
    NOT_FOUND_IN_FILE = 5
    NO_CHANGE = 6
    TOO_LARGE = 7
    BINARY_FILE = 8
    MULTIPLE_MATCHES = 9  # 03 R31
    NOTEBOOK_REQUIRES_NOTEBOOK_EDIT = 10
    REPORT_FILE_FROM_SUBAGENT = 11  # 03 R34
    BLOCKED_SLEEP = 12  # 03 R42
    CONTROL_CHARACTERS = 13  # 03 R44
    OUTSIDE_ALLOWED_DIRS = 14
    UNKNOWN_TOOL = 15
    PERMISSION_DENIED = 16
    INTERRUPTED = 17


@dataclass
class ValidationResult:
    ok: bool = True
    message: str = ""
    error_code: ToolErrorCode = ToolErrorCode.NONE
    behavior: Literal["error", "ask"] = "error"  # "ask" converts failure into a permission prompt (03 R5)

    @staticmethod
    def fail(message: str, code: ToolErrorCode, behavior: Literal["error", "ask"] = "error") -> "ValidationResult":
        return ValidationResult(False, message, code, behavior)


@dataclass
class ContextLayer:
    """A temporary context override requested by a tool (03 R12, 07 §2.3).

    ``kind``: ``allowed_tools`` | ``disallowed_tools`` | ``model`` | ``effort`` |
    ``permission_mode`` | ``cwd``. ``until``: when it expires.
    """

    kind: str
    value: Any
    until: Literal["next_user_message", "session"] = "next_user_message"


@dataclass
class ToolOutput:
    """What ``Tool.call`` returns before mapping to a result block.

    * ``data``: structured output (kept as ``tool_use_result`` for hooks/UIs).
    * ``blocks``: explicit result content (images etc.); default renders ``data``.
    * ``new_messages``: extra conversation items to append (e.g. Skill inline body).
    * ``context_layers``: see ``ContextLayer``.
    * ``notes``: short notices appended to the result text (e.g. formatter note 03 R22).
    """

    data: Any = None
    blocks: list[ContentBlock] | None = None
    is_error: bool = False
    new_messages: list[ConversationItem] = field(default_factory=list)
    context_layers: list[ContextLayer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    end_turn: Literal["completed", "tool_deferred", "hook_stopped"] | None = None  # 01 R7


@dataclass
class ToolCallContext:
    """Fresh per-call view (03 §2.1 ``create(call_ctx)``)."""

    session: "SessionContext"
    tool_use_id: str
    abort: AbortSignal
    siblings: list[ToolUseBlock] = field(default_factory=list)
    assistant_uuid: str | None = None
    progress: Callable[[Any], None] = lambda _data: None
    permission_decision: Decision | None = None  # filled by the pipeline after permissions


class Tool:
    """Base class: override class attributes and the methods you need."""

    # ---- declaration (03 §2.1)
    name: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    output_schema: ClassVar[dict[str, Any] | None] = None
    search_hint: ClassVar[str] = ""
    max_result_chars: ClassVar[int] = 100_000
    persistence_ceiling: ClassVar[int | None] = None
    skip_batch_budget: ClassVar[bool] = False
    backgrounding: ClassVar[Literal["never", "self", "detach"]] = "never"
    should_defer: ClassVar[bool] = False
    always_load: ClassVar[bool] = False
    rule_content_field: ClassVar[Literal["file_path", "command", "path", "url", "query"] | None] = None
    validation_error_steer: ClassVar[str | None] = None
    is_mcp: ClassVar[bool] = False
    kind: ClassVar[Literal["read", "search", "edit", "execute", "web", "agent", "other"]] = "other"
    opts_out_of_bypass: ClassVar[bool] = False
    main_agent_only: ClassVar[bool] = False

    # ---- text
    def description(self, ctx: "SessionContext | None" = None) -> str:
        """Short description."""
        return self.search_hint or self.name

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        """Long usage guidance; lean vs classic variant (03 R66). Authors write their own text."""
        return self.description()

    # ---- predicates (all receive the *parsed* input; 03 R9)
    def is_enabled(self, ctx: "SessionContext | None" = None) -> bool:
        return True

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def is_destructive(self, input: dict[str, Any]) -> bool:
        return False

    def requires_user_interaction(self) -> bool:
        return False

    def get_path(self, input: dict[str, Any]) -> str | None:
        if self.rule_content_field in ("file_path", "path"):
            v = input.get(self.rule_content_field)
            return v if isinstance(v, str) else None
        return None

    # ---- pipeline hooks
    def coerce_input(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Repair near-miss shapes; return ``(input, notes)`` (03 R3)."""
        return raw, []

    def validate_input(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ValidationResult:
        return ValidationResult()

    def check_permissions(self, input: dict[str, Any], call_ctx: ToolCallContext) -> Decision:
        """Tool-specific permission logic (parsers, paths, safety checks).
        Default: passthrough - the global pipeline decides (03 §2.1)."""
        return Decision.passthrough()

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        raise NotImplementedError(f"{type(self).__name__}.call")

    def map_result(self, out: ToolOutput, tool_use_id: str) -> ToolResultBlock:
        """Default: explicit blocks, else text of ``data`` (JSON for non-strings) + notes."""
        if out.blocks is not None:
            content: Any = list(out.blocks)
            if out.notes:
                content.append(TextBlock("\n".join(out.notes)))
            return ToolResultBlock(tool_use_id, content, out.is_error)
        if isinstance(out.data, str) or out.data is None:
            text = out.data or ""
        else:
            text = json.dumps(out.data, indent=2, default=str)
        if out.notes:
            text = (text + "\n\n" if text else "") + "\n".join(out.notes)
        return ToolResultBlock(tool_use_id, text, out.is_error)

    def classifier_projection(self, input: dict[str, Any]) -> str:
        """Compact view for the auto-mode judge (04 §4.5). Empty = tool-name only."""
        return ""

    # ---- API schema
    def api_schema(self, model_id: str = "", lean: bool = True) -> dict[str, Any]:
        return {"name": self.name, "description": self.prompt(model_id, lean), "input_schema": self.input_schema}

    def __repr__(self) -> str:
        return f"<Tool {self.name}>"
