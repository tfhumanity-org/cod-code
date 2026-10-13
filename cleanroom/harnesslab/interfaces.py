"""Cross-package seams as Protocols (FINAL - architect).

Each Protocol here is a contract between two work packages. The *producer* WP
implements it; the *consumer* WP depends only on the Protocol, so the two can be
built in parallel and tested with fakes. Concrete defaults are wired together in
``loop.query.default_deps`` and ``session.Engine`` (WP1).

| Protocol             | Implemented by (WP)                     | Consumed by           |
|----------------------|-----------------------------------------|-----------------------|
| ToolExecutor         | tools.executor.StreamingToolExecutor (2)| loop.query (1)        |
| CanUseTool           | permissions.pipeline.make_can_use_tool(3)| tools.pipeline (2)   |
| PermissionPrompter   | host.broker / tests / cli (5, 1)        | permissions (3)       |
| HookRunner           | hooks.runner.HookRunner (3)             | loop (1), tools (2), compaction (4), agents (5) |
| AttachmentSource     | context.attachments.AttachmentPipeline(4)| loop.query (1)       |
| Compactor            | compaction.controller.AutoCompactor (4) | loop.query (1)        |
| SystemPromptBuilder  | context.prompt.SystemPromptBuilder (4)  | api.request (1)       |
| Classifier           | permissions.classifier.TwoStageClassifier (3) | permissions.modes (3) |
| SubagentRunner       | agents.launcher.SubagentLauncher (5)    | agents.agent_tool, extensions.skill_tool (5) |

Spec references: 01 R2 (loop step order, each step is one seam), 01#T4
(executor), 04 §4.1 (can-use-tool), 02 R31 (attachments), 05 §4.2 (compactor),
02 §4 (system prompt), 04 §4.4 (classifier), 06 §4.1 (subagents).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator, Literal, Protocol, runtime_checkable

from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.model.base import SystemBlock
from harnesslab.permissions.types import (
    ClassifierVerdict,
    Decision,
    PermissionAnswer,
    PermissionPromptRequest,
)
from harnesslab.types import (
    Attachment,
    ConversationItem,
    LoopEvent,
    Message,
    SyntheticReason,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from harnesslab.runtime import SessionContext
    from harnesslab.tools.base import ContextLayer, Tool, ToolCallContext


# ------------------------------------------------------------------ tools


@runtime_checkable
class ToolExecutor(Protocol):
    """Streaming tool scheduler (01 R9-R12, 03 R9-R13, 03 §4.1)."""

    def add(self, block: ToolUseBlock) -> None:
        """Queue a completed tool_use block; start it if scheduling allows."""
        ...

    def drain_completed(self) -> Iterator[LoopEvent]:
        """Yield finished results *in original order* without blocking; an
        unfinished unsafe call is a barrier (01 R10)."""
        ...

    def drain_all(self) -> Iterator[LoopEvent]:
        """Block until every queued call finished; yield remaining results in order."""
        ...

    def discard(self, reason: SyntheticReason) -> list[Message]:
        """Abort everything still pending and return synthetic error results so that
        every tool_use stays paired (03 R13, 03#T2)."""
        ...

    def results(self) -> list[Message]:
        """All tool-result user messages produced so far, in call order."""
        ...

    def context_layers(self) -> list["ContextLayer"]:
        """Context mutations requested by tools, applied per 03 R12."""
        ...

    @property
    def has_pending(self) -> bool: ...


# ------------------------------------------------------------------ permissions


class CanUseTool(Protocol):
    """Full permission decision for one call: hooks -> rules -> mode -> classifier ->
    (host prompt). Returns allow or deny (never ask) (04 §4.1)."""

    def __call__(self, tool: "Tool", input: dict[str, Any], call_ctx: "ToolCallContext") -> Decision: ...


class PermissionPrompter(Protocol):
    """Asks a human (terminal, GUI card, host ``can_use_tool``) (04 §2.10, 04 R47)."""

    def __call__(self, request: PermissionPromptRequest) -> PermissionAnswer: ...


class Classifier(Protocol):
    """Auto-mode judge (04 §4.4). Must fail closed (04 R21)."""

    def classify(self, tool: "Tool", input: dict[str, Any], ctx: "SessionContext") -> ClassifierVerdict: ...


# ------------------------------------------------------------------ hooks


class HookRunner(Protocol):
    def run(self, event: HookEvent, payload: dict[str, Any], ctx: "SessionContext") -> AggregatedHookResult:
        """Run every matching hook for ``event`` in parallel and merge (04 §4.7)."""
        ...


# ------------------------------------------------------------------ context


@dataclass
class TurnInfo:
    """What attachment producers may look at (read-only view of loop state)."""

    items: list[ConversationItem]
    turn_count: int
    is_first_turn: bool = False
    user_prompt: str | None = None
    tokens_used: int = 0
    total_token_budget: int | None = None


class AttachmentSource(Protocol):
    def collect(self, ctx: "SessionContext", info: TurnInfo) -> list[Attachment]:
        """Run all producers under the shared deadline; failures yield nothing (02 R31)."""
        ...


class SystemPromptBuilder(Protocol):
    def build(self, ctx: "SessionContext", tools: list["Tool"]) -> list[SystemBlock]:
        """System prompt blocks with the cache boundary applied (02 §4)."""
        ...


# ------------------------------------------------------------------ compaction


@dataclass
class CompactResult:
    """Output of any compaction (05 R22 ordering: boundary, summary, kept, attachments, hooks)."""

    items: list[ConversationItem]
    trigger: Literal["auto", "manual", "reactive", "precomputed", "micro"]
    pre_tokens: int = 0
    post_tokens: int = 0
    boundary: Message | None = None
    notices: list[str] = field(default_factory=list)


class Compactor(Protocol):
    def maybe_autocompact(self, items: list[ConversationItem], ctx: "SessionContext") -> CompactResult | None:
        """Per-turn controller (05 §4.2). ``None`` = nothing done."""
        ...

    def reactive(
        self, items: list[ConversationItem], ctx: "SessionContext", token_gap: int | None
    ) -> CompactResult | None:
        """After prompt-too-long (01 R26, 05 R26). ``None`` = impossible."""
        ...

    def thrashing(self, ctx: "SessionContext") -> bool:
        """True when the rapid-refill breaker tripped (01 R27, 05 R13)."""
        ...


# ------------------------------------------------------------------ agents


class SubagentRunner(Protocol):
    def run_subagent(
        self,
        ctx: "SessionContext",
        *,
        prompt: str,
        agent_type: str = "general-purpose",
        background: bool = False,
        description: str = "",
    ) -> dict[str, Any]:
        """Launch a nested query loop; returns the Agent tool output union (06 §2.3)."""
        ...
