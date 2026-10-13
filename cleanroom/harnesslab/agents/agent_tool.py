"""Agent, SendMessage and TaskStop tools (WP5).

Implements spec 06 §2.2-§2.3 (Agent input/output; read-only = concurrency-safe =
true; 100 000-char result cap; ``backgrounding = "self"``), R12 (async launch
text), R28 (SendMessage to a finished agent resumes it), R29 (TaskStop with
legacy aliases ``KillShell``/``KillBash``; accepts task id, teammate name or agent
name). Delegates to ``agents.launcher.SubagentLauncher`` and ``ctx.tasks``.

The launcher instance is shared per session through
``ctx.extra["subagent_launcher"]`` (created on first use) so that SendMessage
and TaskStop can find agents the Agent tool started. Tool results are text for
the model (``blocks``) plus the structured 06 §2.3 union in ``data`` for hosts.

Design choices
--------------
* ``name`` must match ``^[A-Za-z][A-Za-z0-9_-]{0,63}$`` and may not be one of the
  reserved names or look like a generated id (06 §2.2).
* ``SendMessage`` to ``"main"`` from a subagent queues a notification for the
  main thread; to a running agent it is delivered as mid-turn input (01 R54).
"""

from __future__ import annotations

import re
from typing import Any

from harnesslab.agents.launcher import (
    AGENT_RESULT_CAP,
    AgentAsyncLaunched,
    AgentInput,
    AgentRunError,
    LaunchRefused,
    SubagentLauncher,
    async_launch_text,
    completed_text,
)
from harnesslab.agents.notifications import notification_message
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput
from harnesslab.types import TextBlock

RESERVED_NAMES = frozenset({"main", "team-lead", "user", "system"})
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ID_SHAPED = re.compile(r"^(a|b|m|p|c|w|wf_)[0-9a-f]{8}$")


def get_launcher(ctx: SessionContext) -> SubagentLauncher:
    """The session's shared launcher (created lazily)."""
    launcher = ctx.extra.get("subagent_launcher")
    if launcher is None:
        launcher = SubagentLauncher()
        ctx.extra["subagent_launcher"] = launcher
    return launcher


def _text_output(text: str, data: Any = None, *, is_error: bool = False) -> ToolOutput:
    return ToolOutput(data=data if data is not None else text, blocks=[TextBlock(text)], is_error=is_error)


def validate_agent_name(name: str) -> str | None:
    """Return an error message, or ``None`` if ``name`` is acceptable (06 §2.2)."""
    if not _NAME_RE.match(name):
        return "Agent names must start with a letter and use only letters, digits, '-' and '_' (max 64)."
    if name.lower() in RESERVED_NAMES or _ID_SHAPED.match(name):
        return f"{name!r} is reserved; choose a descriptive name."
    return None


class AgentTool(Tool):
    name = "Agent"
    aliases = ("Task",)
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string"},
            "subagent_type": {"type": "string"},
            "model": {"type": "string"},
            "effort": {"type": "string"},
            "run_in_background": {"type": "boolean"},
            "name": {"type": "string"},
            "isolation": {"type": "string", "enum": ["worktree", "remote"]},
        },
        "required": ["description", "prompt"],
    }
    backgrounding = "self"
    kind = "agent"
    search_hint = "delegate a task to a subagent"
    max_result_chars = AGENT_RESULT_CAP

    def __init__(self, launcher: SubagentLauncher | None = None) -> None:
        self.launcher = launcher

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        return (
            "Start a subagent to work on a self-contained task. Unless told otherwise, a launched agent "
            "works asynchronously: its id comes back immediately and a notification follows on completion. Until that "
            "notification arrives you know nothing about the result - never guess or invent it. Pass "
            "run_in_background: false when you need the answer before you can continue. Brief the agent "
            "fully: it cannot see this conversation. Use SendMessage with the returned id to continue an "
            "agent later instead of starting a new one."
        )

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        launcher = self.launcher or get_launcher(ctx)
        ctx.extra.setdefault("subagent_launcher", launcher)
        name = input.get("name")
        if name:
            err = validate_agent_name(name)
            if err:
                return _text_output(err, is_error=True)
        inp = AgentInput(
            description=input.get("description", ""),
            prompt=input.get("prompt", ""),
            subagent_type=input.get("subagent_type") or "general-purpose",
            model=input.get("model"),
            effort=input.get("effort"),
            run_in_background=input.get("run_in_background"),
            name=name,
            isolation=input.get("isolation"),
            cwd=input.get("cwd"),
        )
        try:
            res = launcher.launch(ctx, inp, parent_items=ctx.extra.get("current_items") or ctx.extra.get("conversation_items"))
        except (LaunchRefused, AgentRunError) as e:
            return _text_output(str(e), is_error=True)
        if isinstance(res, AgentAsyncLaunched):
            data = {"status": "async_launched", "agentId": res.agent_id, "outputFile": res.output_file, "canReadOutputFile": False, "sharesCwd": res.shares_cwd}
            return _text_output(async_launch_text(res), data)
        data = {
            "status": "completed",
            "content": res.content,
            "usage": res.usage,
            "totalTokens": res.total_tokens,
            "toolStats": {"toolUses": res.tool_uses, "durationMs": res.duration_ms},
            "canContinueAgent": res.agent_id is not None,
            "agentId": res.agent_id,
        }
        return _text_output(completed_text(res), data)


class SendMessageTool(Tool):
    name = "SendMessage"
    input_schema = {
        "type": "object",
        "properties": {"to": {"type": "string"}, "message": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["to", "message"],
    }
    should_defer = True
    search_hint = "send a follow-up message to a running or finished agent"

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        to, message = str(input.get("to", "")).strip(), str(input.get("message", ""))
        if to == "main":
            if ctx.is_main_agent:
                return _text_output("You are the main agent; there is nobody called 'main' to message.", is_error=True)
            if ctx.notifications is None:
                return _text_output("No message channel to the main agent is available.", is_error=True)
            ctx.notifications.push(notification_message(f"Message from agent {ctx.agent_id}:\n{message}"))
            return _text_output("Message queued for the main agent.")
        launcher = get_launcher(ctx)
        if launcher.deliver_message(to, message):
            return _text_output(f"Message delivered to running agent {to}; it will see it at its next step.")
        try:
            res = launcher.continue_agent(ctx, to, message)
        except (LaunchRefused, AgentRunError) as e:
            return _text_output(str(e), is_error=True)
        if isinstance(res, AgentAsyncLaunched):
            return _text_output(f"Agent {to} resumed with your message.\n" + async_launch_text(res), {"status": "async_launched", "agentId": res.agent_id})
        return _text_output(completed_text(res), {"status": "completed", "agentId": res.agent_id, "content": res.content})


class TaskStopTool(Tool):
    name = "TaskStop"
    aliases = ("KillShell", "KillBash")
    input_schema = {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    search_hint = "stop a running background task or agent"

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        ref = str(input.get("task_id", "")).strip()
        if ctx.tasks is None:
            return _text_output("There are no background tasks in this session.", is_error=True)
        task = ctx.tasks.get(ref)
        launcher = ctx.extra.get("subagent_launcher")
        if launcher is not None:
            rec = launcher.record(ref)
            if rec is not None and rec.running and rec.task_id:
                task = ctx.tasks.get(rec.task_id)
        if task is None:
            running = ", ".join(f"{t.id} ({t.description})" for t in ctx.tasks.list(status="running")) or "none"
            return _text_output(f"No task with id or name {ref!r}. Running tasks: {running}.", is_error=True)
        if task.status != "running":
            return _text_output(f"Task {task.id} is not running (status: {task.status}); nothing to stop.", is_error=True)
        ctx.tasks.stop(task.id)
        note = " It can be continued later with SendMessage." if task.kind == "agent" else ""
        return _text_output(f"Stopped task {task.id} ({task.description}).{note}", {"task_id": task.id, "status": "killed"})
