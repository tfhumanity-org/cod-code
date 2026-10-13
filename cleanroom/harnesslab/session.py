"""Engine facade: wires every subsystem into one session (WP1).

This is the "one engine, many front-ends" entry point (01#T12, 07#T8): the CLI
REPL, the NDJSON host protocol (``host.protocol``) and tests all drive an
``Engine``.

Responsibilities
----------------
* ``create_session_context`` builds a ``SessionContext`` with the real
  collaborators: ``tools.ledger.ReadLedger``, ``tools.builtin.default_tools``,
  ``hooks.runner.HookRunner``, ``permissions.pipeline.make_can_use_tool``,
  ``transcript.store.TranscriptStore``, ``transcript.checkpoints.FileHistory``,
  ``agents.tasks.TaskRegistry`` + ``agents.notifications.NotificationQueue``.
  All imports of other WPs' modules are local to the function.
* ``Engine.submit(prompt)`` runs UserPromptSubmit hooks (prompt erased on a
  block, 04 R33), snapshots files for rewind (05 R46), appends the user row
  and yields events from ``loop.query.run_query``.
* ``Engine.interrupt()`` aborts the current turn (01 R51).
* ``Engine.resume(session_id, at_uuid=None, fork=False)`` (05 §4.7, 05 R42).

Design choices (spec silent):

* A fresh ``AbortController`` is installed on ``ctx.abort`` at the start of every
  ``submit`` ("one abort controller per turn", 01 R51).
* File checkpointing (05 R46) is on by default, off with
  ``CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING`` or setting
  ``fileCheckpointingEnabled: false``, and opt-in for SDK sessions via
  ``CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING``.
* After a turn the engine adopts ``ctx.extra["loop_items"]`` (published by
  ``run_query``) as its history, so compaction rewrites are kept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping

from harnesslab.agents.notifications import NotificationQueue
from harnesslab.agents.tasks import TaskRegistry
from harnesslab.config import Settings, env_truthy
from harnesslab.hooks.types import HookEvent
from harnesslab.interfaces import PermissionPrompter
from harnesslab.loop.state import QueryDeps, QueryParams
from harnesslab.model.base import Model
from harnesslab.permissions.types import PermissionMode
from harnesslab.runtime import SessionContext
from harnesslab.types import ConversationItem, LoopEvent, Message, MessageEvent, SystemNoticeEvent, TerminalReason
from harnesslab.util.abort import AbortController
from harnesslab.util.text import wrap_system_reminder


def checkpointing_enabled(ctx: SessionContext) -> bool:
    """05 R46."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING"):
        return False
    if ctx.settings.get("fileCheckpointingEnabled", True) is False:
        return False
    if ctx.is_sdk:
        return env_truthy(ctx.env, "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING")
    return True


def create_session_context(
    model: Model,
    *,
    cwd: Path,
    config_dir: Path,
    settings: Settings | None = None,
    env: Mapping[str, str] | None = None,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    prompter: PermissionPrompter | None = None,
    tools: list[Any] | None = None,
    skills: list[Any] | None = None,
    persist: bool = True,
) -> SessionContext:
    """A main-session context with every real collaborator attached."""
    from harnesslab.hooks.runner import HookRunner, load_hook_config
    from harnesslab.permissions.pipeline import make_can_use_tool
    from harnesslab.tools.builtin import default_tools
    from harnesslab.tools.ledger import ReadLedger
    from harnesslab.transcript.checkpoints import FileHistory
    from harnesslab.transcript.store import TranscriptStore

    settings = settings or Settings()
    ctx = SessionContext(
        cwd=Path(cwd),
        config_dir=Path(config_dir),
        env=dict(env or {}),
        settings=settings,
        model=model,
        model_id=getattr(model, "name", "model"),
    )
    ctx.permissions.mode = permission_mode
    ctx.prompter = prompter
    ctx.tasks = TaskRegistry()
    ctx.notifications = NotificationQueue()
    ctx.ledger = ReadLedger()
    ctx.tools = list(tools) if tools is not None else default_tools()
    ctx.hook_runner = HookRunner(load_hook_config(settings), settings=settings)
    ctx.can_use_tool = make_can_use_tool(ctx)
    ctx.transcript = TranscriptStore(ctx.transcript_path, session_id=ctx.session_id, cwd=str(ctx.cwd), enabled=persist)
    if checkpointing_enabled(ctx):
        ctx.file_history = FileHistory(ctx.file_history_dir, enabled=persist)
    if skills is None:
        from harnesslab.extensions.skills import discover_skills, session_skill_roots

        try:
            skills = discover_skills(session_skill_roots(ctx.cwd, ctx.config_dir, ctx.additional_dirs))
        except Exception:  # a broken skill directory must not stop the session
            skills = []
    ctx.skills = list(skills)
    ctx.tasks.on_finish(_fallback_notifier(ctx))
    return ctx


def _fallback_notifier(ctx: SessionContext) -> Any:
    """06 §4.4: a finished background task reaches the model exactly once.
    Producers that notify themselves (agents, MCP, background shells) tag the
    task ``meta["notified"]``; a stopped task was already reported by TaskStop.
    Anything else gets a generic notification here."""

    def on_finish(task: Any) -> None:
        if task.meta.get("notified") or task.status == "killed" or ctx.notifications is None:
            return
        task.meta["notified"] = True
        summary = f"Background {task.kind} task \"{task.description}\" {task.status}."
        ctx.notifications.notify(task.id, task.status, summary, task.error or None)

    return on_finish


class Engine:
    def __init__(self, ctx: SessionContext, deps: QueryDeps | None = None, params: QueryParams | None = None) -> None:
        self.ctx = ctx
        self.items: list[ConversationItem] = []
        self.last_reason: TerminalReason | None = None
        self.deps = deps
        self.params = params

    def _params(self) -> QueryParams:
        if self.params is not None:
            return self.params
        c = self.ctx
        return QueryParams(
            max_turns=c.max_turns,
            max_budget_usd=c.max_budget_usd,
            query_source="sdk" if c.is_sdk else "main",
            is_interactive=c.is_interactive,
            keep_partial_on_abort=c.is_sdk or bool(c.settings.get("keepPartialMessageOnAbort", False)),
        )

    def submit(self, prompt: str) -> Iterator[LoopEvent]:
        """Run one user turn; yields every loop event. ``self.last_reason`` holds the
        terminal reason afterwards (``None`` if a hook erased the prompt)."""
        from harnesslab.loop.query import default_deps, hook_payload, run_query

        ctx = self.ctx
        ctx.abort = AbortController()  # one controller per turn (01 R51)
        from harnesslab.loop.context_layers import expire_layers

        expire_layers(ctx)  # Skill grants etc. last until the next user message (07 R8)
        self.last_reason = None
        extra_context: list[ConversationItem] = []

        if ctx.hook_runner is not None:  # 04 R33
            res = ctx.hook_runner.run(
                HookEvent.USER_PROMPT_SUBMIT, hook_payload(HookEvent.USER_PROMPT_SUBMIT, ctx, prompt=prompt), ctx
            )
            if res.blocked or res.prevent_continuation:
                reason = "; ".join(res.blocking_errors) or res.decision_reason or res.stop_reason or "blocked by hook"
                yield SystemNoticeEvent("prompt_blocked", f"Prompt blocked by a UserPromptSubmit hook: {reason}")
                return
            for text in res.additional_contexts:
                extra_context.append(Message.user(wrap_system_reminder(text), is_meta=True, origin="hook"))

        user = Message.user(prompt, origin="user")
        if ctx.file_history is not None:  # 05 R46: snapshot per prompt
            ctx.file_history.snapshot(user.uuid)
        for it in [user, *extra_context]:
            self.items.append(it)
            if ctx.transcript is not None:
                ctx.transcript.append(it)
            yield MessageEvent(it)

        if self.deps is None:
            self.deps = default_deps(ctx)
        ctx.extra.pop("loop_items", None)
        reason = yield from run_query(list(self.items), ctx, self._params(), self.deps)
        self.items = list(ctx.extra.get("loop_items") or self.items)
        self.last_reason = reason

    def run(self, prompt: str) -> list[LoopEvent]:
        """Convenience: drive ``submit`` to completion and return all events."""
        return list(self.submit(prompt))

    def interrupt(self) -> None:
        """01 R51: abort the turn in flight (tools get child signals)."""
        self.ctx.abort.abort("interrupted")

    @classmethod
    def resume(cls, ctx: SessionContext, *, at_uuid: str | None = None, fork: bool = False) -> "Engine":
        """05 §4.7: load ``ctx.session_id``'s transcript (optionally at ``at_uuid``);
        ``fork`` continues under a new session id (05 R42, R43)."""
        from harnesslab.transcript.store import TranscriptStore, load_session

        loaded = load_session(ctx.transcript_path, at_uuid=at_uuid, fork=fork)
        if loaded.session_id != ctx.session_id:
            ctx.session_id = loaded.session_id
            if ctx.transcript is not None:
                ctx.transcript = TranscriptStore(
                    ctx.transcript_path, session_id=ctx.session_id, cwd=str(ctx.cwd),
                    enabled=getattr(ctx.transcript, "enabled", True),
                )
        ctx.extra["resumed"] = True  # pairing repair may drop a leading orphan (01 R56)
        engine = cls(ctx)
        engine.items = list(loaded.items)
        return engine
