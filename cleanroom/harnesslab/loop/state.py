"""Loop state, guards, parameters and injectable dependencies (FINAL - architect).

Spec coverage
-------------
* 01 §2.1  ``LoopState`` / ``Guards`` / ``QueryTrace``.
* 01 R1    the loop replaces the state object each iteration and records a
           ``TransitionReason`` (``LoopState.next``).
* 01 R3    guards reset on ``next_turn`` (``LoopState.next`` builds fresh
           ``Guards`` exactly then).
* 01 §4    ``recovery_ladder`` returns ``Continue`` or ``Terminal``.

``QueryDeps`` gathers every collaborator of ``loop.query.run_query`` so tests can
substitute fakes for any subsystem (compaction, attachments, hooks, executor,
retrying model call) while the real ones are still being implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Iterator

from harnesslab.types import ConversationItem, LoopEvent, Message, TerminalReason, TransitionReason, new_uuid

if TYPE_CHECKING:
    from harnesslab.interfaces import AttachmentSource, Compactor, HookRunner, SystemPromptBuilder, ToolExecutor
    from harnesslab.model.base import ModelRequest, StreamEvent
    from harnesslab.runtime import SessionContext

#: 01 R28 / §5 output-limit recovery nudges per round.
MAX_OUTPUT_LIMIT_RECOVERIES = 3
#: 01 R6 default stop-hook block cap (``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP``).
DEFAULT_STOP_HOOK_BLOCK_CAP = 8


@dataclass
class Guards:
    """Per-round recovery budgets (01 §2.1). Reset on every ``next_turn`` (01 R3)."""

    output_limit_recoveries: int = 0
    tried_reactive_compact: bool = False
    reactive_compact_depth: int = 0
    thinking_only_nudged: bool = False
    malformed_tool_retried: bool = False
    truncation_retried: bool = False
    stop_hook_block_count: int = 0


@dataclass
class QueryTrace:
    """Telemetry correlation: depth +1 per loop round (01 §2.1)."""

    chain_id: str = field(default_factory=new_uuid)
    depth: int = 0


@dataclass
class LoopState:
    items: list[ConversationItem]
    guards: Guards = field(default_factory=Guards)
    transition: TransitionReason | None = None
    turn_count: int = 0
    trace: QueryTrace = field(default_factory=QueryTrace)
    max_tokens_override: int | None = None  # 01 R4 / R20: applies to the next request only
    model_override: str | None = None  # fallback chain position (01 R33)

    def next(self, transition: TransitionReason, append: list[ConversationItem] | None = None, **changes: Any) -> "LoopState":
        """Return the successor state (01 R1). ``next_turn`` resets guards and
        increments ``turn_count`` (01 R3)."""
        items = self.items + list(append or [])
        if transition == TransitionReason.NEXT_TURN:
            return LoopState(
                items=items,
                guards=Guards(),
                transition=transition,
                turn_count=self.turn_count + 1,
                trace=QueryTrace(self.trace.chain_id, self.trace.depth + 1),
                **changes,
            )
        return replace(
            self,
            items=items,
            transition=transition,
            trace=QueryTrace(self.trace.chain_id, self.trace.depth + 1),
            **changes,
        )


@dataclass
class Continue:
    """Recovery ladder outcome: iterate again (01 §4 ``recovery_ladder``)."""

    reason: TransitionReason
    extra: list[ConversationItem] = field(default_factory=list)
    tombstone: Message | None = None  # retract this assistant row first (01 R30)
    replace_items: list[ConversationItem] | None = None  # e.g. post-compaction history
    keep_assistant: bool = True  # append the (withheld) assistant row before ``extra``


@dataclass
class Terminal:
    reason: TerminalReason
    surface: Message | None = None  # withheld API-error row to finally show (01 #T2)


@dataclass
class QueryParams:
    """Static per-query configuration."""

    max_turns: int | None = None
    max_budget_usd: float | None = None
    query_source: str = "main"
    is_subagent: bool = False
    is_interactive: bool = True
    strict_pairing: bool = False  # 01 R56: raise instead of repairing (CI)
    stop_hook_block_cap: int = DEFAULT_STOP_HOOK_BLOCK_CAP
    keep_partial_on_abort: bool = False  # 01 R53
    tool_choice: dict[str, Any] | None = None


@dataclass
class QueryDeps:
    """Injectable collaborators of ``run_query``. ``None`` = feature disabled."""

    #: Streams one request with retries/fallback (api.retry.call_with_retries).
    call_model: Callable[["ModelRequest", "SessionContext"], Iterator["StreamEvent | LoopEvent"]]
    #: Builds the request for this round (api.request.build_model_request).
    build_request: Callable[[LoopState, "SessionContext", QueryParams], "ModelRequest"]
    #: Fresh executor per round (tools.executor.StreamingToolExecutor).
    make_executor: Callable[["SessionContext", str], "ToolExecutor"]
    attachments: "AttachmentSource | None" = None
    compactor: "Compactor | None" = None
    hooks: "HookRunner | None" = None
    system_prompt: "SystemPromptBuilder | None" = None
