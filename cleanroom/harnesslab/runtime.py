"""SessionContext - the per-session (or per-agent) state bag (FINAL - architect).

One ``SessionContext`` is created per main session and per subagent (a child
context is derived with ``SessionContext.child``). Every subsystem receives it
instead of reaching for globals, which keeps the code testable and makes it
obvious what state each feature touches.

Fields are grouped by owning work package. A field typed ``Any`` holds an object
from a module still being implemented; the real type is named in the comment
(imported only for type checking, avoiding import cycles).

Spec coverage
-------------
* 01 §2.1  per-turn abort controller lives here (``abort``).
* 03 §2.3  the read ledger (``ledger``) - subagents get a clone without seeded
           entries (03 §2.3 last paragraph) via ``child``.
* 03 R39   cwd persists across shell calls (``cwd``), may be pinned for subagents.
* 04 §2.1  permission mode / rules (``permissions``).
* 05 §2.6  on-disk layout (``transcript_path``, ``tool_results_dir``,
           ``file_history_dir``) under ``config_dir``.
* 06 R3    agent nesting depth (``agent_depth``).
* 02 R32   turn counters used for throttled nudges (``counters``).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from harnesslab.config import Settings
from harnesslab.permissions.types import PermissionContext
from harnesslab.types import new_uuid
from harnesslab.util.abort import AbortController
from harnesslab.util.ids import project_slug

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from harnesslab.interfaces import CanUseTool, HookRunner, PermissionPrompter
    from harnesslab.model.base import Model


@dataclass
class Counters:
    """Turn-counted nudge state (02 R32 / P08)."""

    assistant_turns: int = 0
    turns_since_todo_write: int = 0
    turns_since_todo_reminder: int = 0
    turns_since_plan_reminder: int = 0
    plan_reminder_count: int = 0


@dataclass
class SessionContext:
    # ---- identity & environment (architect)
    cwd: Path
    session_id: str = field(default_factory=new_uuid)
    original_cwd: Path | None = None
    config_dir: Path = field(default_factory=lambda: Path.home() / ".harnesslab")
    env: Mapping[str, str] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)
    additional_dirs: list[Path] = field(default_factory=list)
    is_interactive: bool = True
    is_sdk: bool = False
    platform: str = "darwin"

    # ---- model / loop (WP1)
    model: "Model | None" = None
    model_id: str = "scripted-model"
    fallback_models: list[str] = field(default_factory=list)
    effort: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    abort: AbortController = field(default_factory=AbortController)
    cost_usd: float = 0.0
    counters: Counters = field(default_factory=Counters)
    pending_user_inputs: list[str] = field(default_factory=list)  # mid-turn input (01 R54)

    # ---- tools (WP2)
    tools: list[Any] = field(default_factory=list)  # list[tools.base.Tool]
    ledger: Any = None  # tools.ledger.ReadLedger
    todos: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # per agent id (03 R57)
    discovered_tools: set[str] = field(default_factory=set)  # deferred tools loaded via ToolSearch
    spill_memo: dict[str, Any] = field(default_factory=dict)  # tool_use_id -> replacement (03 R25)
    pinned_cwd: bool = False

    # ---- permissions & hooks (WP3)
    permissions: PermissionContext = field(default_factory=PermissionContext)
    can_use_tool: "CanUseTool | None" = None
    prompter: "PermissionPrompter | None" = None
    hook_runner: "HookRunner | None" = None
    classifier: Any = None  # interfaces.Classifier
    denial_state: Any = None  # permissions.classifier.DenialTracker

    # ---- context & lifetime (WP4)
    transcript: Any = None  # transcript.store.TranscriptStore
    file_history: Any = None  # transcript.checkpoints.FileHistory
    touched_paths: list[str] = field(default_factory=list)  # for nested instruction files (02 R20)
    invoked_skills: list[Any] = field(default_factory=list)  # re-injected after compaction (05 §4.5)
    plan_file: Path | None = None
    compaction_state: Any = None  # compaction.controller.CompactionState

    # ---- agents & extensions (WP5)
    agent_id: str | None = None  # None = main thread
    agent_type: str | None = None
    agent_depth: int = 0
    is_fork: bool = False
    tasks: Any = None  # agents.tasks.TaskRegistry
    notifications: Any = None  # agents.notifications.NotificationQueue
    skills: list[Any] = field(default_factory=list)  # extensions.skills.Command
    mcp_clients: list[Any] = field(default_factory=list)

    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd)
        if self.original_cwd is None:
            self.original_cwd = self.cwd

    # ---- derived paths (05 §2.6)
    @property
    def is_main_agent(self) -> bool:
        return self.agent_id is None

    @property
    def project_dir(self) -> Path:
        return self.config_dir / "projects" / project_slug(str(self.original_cwd))

    @property
    def transcript_path(self) -> Path:
        if self.agent_id is None:
            return self.project_dir / f"{self.session_id}.jsonl"
        return self.project_dir / self.session_id / "subagents" / f"agent-{self.agent_id}.jsonl"

    @property
    def session_state_dir(self) -> Path:
        return self.project_dir / self.session_id

    @property
    def tool_results_dir(self) -> Path:
        """Spill directory for oversized tool output (03 R24)."""
        return self.session_state_dir / "tool-results"

    @property
    def tasks_dir(self) -> Path:
        """Background task output files (03 R41)."""
        return self.session_state_dir / "tasks"

    @property
    def file_history_dir(self) -> Path:
        return self.config_dir / "file-history" / self.session_id

    def child(self, *, agent_id: str, agent_type: str, **overrides: Any) -> "SessionContext":
        """Derive a subagent context (06 §3 "Subagent context").

        Shares: env, settings, config dir, session id, task registry, notification
        queue, hook runner, prompter. Copies: cwd, permissions. Fresh: abort child,
        counters, todos, discovered tools. The read ledger is cloned without
        seeded entries if the ledger supports ``clone_for_subagent`` (03 §2.3).
        """
        ledger = self.ledger
        if ledger is not None and hasattr(ledger, "clone_for_subagent"):
            ledger = ledger.clone_for_subagent()
        c = SessionContext(
            cwd=self.cwd,
            session_id=self.session_id,
            original_cwd=self.original_cwd,
            config_dir=self.config_dir,
            env=self.env,
            settings=self.settings,
            additional_dirs=list(self.additional_dirs),
            is_interactive=False,
            is_sdk=self.is_sdk,
            platform=self.platform,
            model=self.model,
            model_id=self.model_id,
            fallback_models=list(self.fallback_models),
            effort=self.effort,
            abort=self.abort.child(),
            tools=list(self.tools),
            ledger=ledger,
            permissions=copy.deepcopy(self.permissions),
            can_use_tool=self.can_use_tool,
            prompter=self.prompter,
            hook_runner=self.hook_runner,
            classifier=self.classifier,
            tasks=self.tasks,
            notifications=self.notifications,
            skills=self.skills,
            mcp_clients=self.mcp_clients,
            agent_id=agent_id,
            agent_type=agent_type,
            agent_depth=self.agent_depth + 1,
        )
        for k, v in overrides.items():
            setattr(c, k, v)
        return c
