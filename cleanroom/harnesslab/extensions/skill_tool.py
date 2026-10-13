"""The Skill tool: inline expansion with context layers, or fork (WP5).

Implements spec 07 §2.3 (input ``{skill, args?}``; output inline | forked;
100 000-char cap; never auto-backgrounded), R8 (inline: body expanded into
new meta user messages + context layers ``allowed_tools`` / ``model`` /
``effort`` expiring at the next user message), R9 (fork: subagent via
``ctx``'s ``SubagentRunner``; ``background: true`` returns immediately and
reports via notification), R10 (tool guidance content), 03 R60, 07 R12
(coordinator invocations read-only). Records the skill in
``ctx.invoked_skills`` for post-compaction re-injection (05 §4.5).

Seams
-----
* Skills come from ``ctx.skills`` (``extensions.skills.Command``).
* ``ctx.extra["running_skill"]`` names the skill a forked child is executing;
  the fork-recursion guard (R7) refuses re-invoking it there. For a fork, the
  tool sets ``ctx.extra["pending_skill_fork"]`` around the runner call; the
  launcher copies it into the child as ``running_skill``.
* ``ctx.extra["failed_plugins"]`` (set of plugin names) for the R7 message.
* ``ctx.extra["coordinator"]`` makes invocations read-only (R12): no fork, no
  tool grants, no model/effort layers.
* ``ctx.invoked_skills`` entries are dicts ``{name, body, path}``.
"""

from __future__ import annotations

from typing import Any

from harnesslab.extensions.skills import Command, SkillResolutionError, render_body, resolve_skill
from harnesslab.interfaces import SubagentRunner
from harnesslab.tools.base import ContextLayer, Tool, ToolCallContext, ToolOutput
from harnesslab.types import Message, TextBlock

SKILL_RESULT_CAP = 100_000


class SkillTool(Tool):
    name = "Skill"
    input_schema = {
        "type": "object",
        "properties": {"skill": {"type": "string"}, "args": {"type": "string"}},
        "required": ["skill"],
        "additionalProperties": False,
    }
    search_hint = "invoke a skill"
    max_result_chars = SKILL_RESULT_CAP
    backgrounding = "never"

    def __init__(self, runner: SubagentRunner | None = None) -> None:
        self.runner = runner

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        """07 R10 required content, in our own words."""
        return (
            "Load a skill: a packaged set of instructions for a kind of task.\n"
            "- When a skill in the listing covers the task, call this tool first, before doing the work another way.\n"
            "- Some skills run as their own subagent and return its result; background skills return only an agent name and report later.\n"
            "- A user typing /name is asking you to run that skill.\n"
            "- Only names from the skill listing, or typed by the user, are valid. Built-in CLI commands are not skills.\n"
            "- If both a scoped (dir:name) and an unscoped name exist, prefer the most specific scope that matches the files you are working on.\n"
            "- If the skill's instructions are already in your context this turn, follow them instead of invoking it again."
        )

    def _runner(self, call_ctx: ToolCallContext) -> SubagentRunner:
        if self.runner is not None:
            return self.runner
        from harnesslab.agents.agent_tool import get_launcher  # own WP

        return get_launcher(call_ctx.session)

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        coordinator = bool(ctx.extra.get("coordinator"))
        try:
            cmd: Command = resolve_skill(
                str(input.get("skill", "")),
                list(ctx.skills),
                running_skill=ctx.extra.get("running_skill"),
                failed_plugins=set(ctx.extra.get("failed_plugins") or ()),
            )
        except SkillResolutionError as e:
            return ToolOutput(data=str(e), is_error=True)
        args = str(input.get("args") or "")
        body = render_body(cmd, args)
        ctx.invoked_skills.append({"name": cmd.name, "body": body, "path": str(cmd.path) if cmd.path else None})

        if cmd.context == "fork" and not coordinator:
            return self._fork(cmd, body, call_ctx)

        # ---- inline (R8): body becomes a meta user message; temporary layers until next user message
        new_messages = [Message.user(f"Instructions from skill '{cmd.name}':\n\n{body}", is_meta=True, origin="skill")]
        layers: list[ContextLayer] = []
        if not coordinator:
            if cmd.allowed_tools:
                layers.append(ContextLayer("allowed_tools", list(cmd.allowed_tools)))
            if cmd.disallowed_tools:
                layers.append(ContextLayer("disallowed_tools", list(cmd.disallowed_tools)))
            if cmd.model and cmd.model != "inherit":
                layers.append(ContextLayer("model", cmd.model))
            if cmd.effort is not None:
                layers.append(ContextLayer("effort", cmd.effort))
        data: dict[str, Any] = {"status": "inline", "skill": cmd.name}
        if cmd.allowed_tools and not coordinator:
            data["allowedTools"] = list(cmd.allowed_tools)
        if cmd.model and not coordinator:
            data["model"] = cmd.model
        if coordinator:
            data["readOnly"] = True
        return ToolOutput(
            data=data,
            blocks=[TextBlock(f"Skill '{cmd.name}' loaded; its instructions follow. Carry them out now.")],
            new_messages=new_messages,
            context_layers=layers,
        )

    def _fork(self, cmd: Command, body: str, call_ctx: ToolCallContext) -> ToolOutput:
        """R9: run the skill as a subagent; marked so the child cannot re-invoke it."""
        ctx = call_ctx.session
        runner = self._runner(call_ctx)
        ctx.extra["pending_skill_fork"] = cmd.name
        try:
            res = runner.run_subagent(
                ctx,
                prompt=body,
                agent_type=cmd.agent or "general-purpose",
                background=cmd.background,
                description=f"skill {cmd.name}",
            )
        except Exception as e:  # noqa: BLE001 - launcher refusals are model-readable
            return ToolOutput(data=f"Skill '{cmd.name}' could not start its subagent: {e}", is_error=True)
        finally:
            ctx.extra.pop("pending_skill_fork", None)
        agent_id = res.get("agentId")
        if res.get("status") == "async_launched":
            text = (
                f"Skill '{cmd.name}' is running in the background as agent {agent_id}. "
                "Its result will arrive as a task notification; do not guess it."
            )
            return ToolOutput(data={"status": "forked", "agentId": agent_id, "result": "", "background": True}, blocks=[TextBlock(text)])
        result = str(res.get("content", res.get("text", "")))[:SKILL_RESULT_CAP]
        return ToolOutput(data={"status": "forked", "agentId": agent_id, "result": result}, blocks=[TextBlock(result or "(the skill returned no text)")])
