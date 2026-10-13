"""Focused tests for the cross-package seams fixed during integration
(WORKPLAN "Change requests"). Each test names the request it pins down."""

from __future__ import annotations

import sys

import pytest

from harnesslab.hooks.types import AggregatedHookResult
from harnesslab.model.base import SystemBlock
from harnesslab.model.scripted import ScriptedModel
from harnesslab.permissions.types import PermissionAnswer, PermissionMode
from harnesslab.session import create_session_context
from harnesslab.tools.base import ContextLayer, ToolCallContext
from harnesslab.types import Attachment, Message, ToolUseBlock
from harnesslab.util.abort import AbortController
from harnesslab.util.text import REMINDER_OPEN


def _session(project, config_dir, **kw):
    return create_session_context(ScriptedModel(), cwd=project, config_dir=config_dir, **kw)


def _cc(ctx, tool_use_id="t1", siblings=None):
    return ToolCallContext(session=ctx, tool_use_id=tool_use_id, abort=AbortController().signal, siblings=siblings or [])


def _tool(ctx, name):
    return next(t for t in ctx.tools if t.name == name)


class _Hooks:
    """A HookRunner stand-in returning one canned PreToolUse result."""

    def __init__(self, result: AggregatedHookResult) -> None:
        self.result = result

    def run(self, event, payload, ctx):
        return self.result if event.value == "PreToolUse" else AggregatedHookResult()


# (a) reminders are wrapped exactly once and renderers are registered by normalize
def test_attachment_rendered_with_single_envelope():
    from harnesslab.loop.normalize import to_api_messages

    msgs, _ = to_api_messages([Message.user("hi"), Attachment("date_change", {"date": "2030-01-01"}), Attachment("rewind", {"text": "x"})])
    texts = [b["text"] for m in msgs for b in m["content"] if b.get("type") == "text"]
    assert all(t.count(REMINDER_OPEN) <= 1 for t in texts)
    assert sum(REMINDER_OPEN in t for t in texts) >= 1


# (c) one copy of the 04 R30 guidance per denial
def test_denial_guidance_appears_once(project, config_dir):
    from harnesslab.tools.pipeline import run_tool_use

    ctx = _session(project, config_dir, prompter=lambda r: PermissionAnswer("deny", message="nope"))
    out = run_tool_use(ToolUseBlock("t1", "Write", {"file_path": str(project / "a.txt"), "content": "x"}), _tool(ctx, "Write"), _cc(ctx))
    text = out.result.tool_results()[0].text()
    assert out.denied and text.count("do not try to get around") + text.count("works around this denial") == 1


# (d) read-only tools inside the working directories never prompt; outside they do
def test_read_only_tools_auto_allowed_inside_workdir(project, config_dir, tmp_path):
    from harnesslab.permissions.pipeline import make_can_use_tool

    asked = []
    ctx = _session(project, config_dir, prompter=lambda r: asked.append(r.tool_name) or PermissionAnswer("deny"))
    can = make_can_use_tool(ctx)
    assert can(_tool(ctx, "Glob"), {"pattern": "*.py"}, _cc(ctx)).behavior == "allow"
    assert can(_tool(ctx, "Read"), {"file_path": str(project / "x.py")}, _cc(ctx)).behavior == "allow"
    assert asked == []
    outside = tmp_path / "elsewhere.txt"
    assert can(_tool(ctx, "Read"), {"file_path": str(outside)}, _cc(ctx)).behavior == "deny"
    assert asked == ["Read"]


# (b)+(j) PreToolUse runs once (inside can_use_tool); "defer" uses the shared convention
def test_hook_defer_honoured_only_headless_and_alone(project, config_dir):
    from harnesslab.tools.pipeline import run_tool_use

    ctx = _session(project, config_dir, permission_mode=PermissionMode.BYPASS)
    ctx.hook_runner = _Hooks(AggregatedHookResult(decision="defer", hook_name="h"))
    block = ToolUseBlock("t1", "Glob", {"pattern": "*"})
    interactive = run_tool_use(block, _tool(ctx, "Glob"), _cc(ctx, siblings=[block]))
    assert interactive.denied and interactive.end_turn is None  # behaves like deny
    ctx.permissions.headless = True
    alone = run_tool_use(block, _tool(ctx, "Glob"), _cc(ctx, siblings=[block]))
    assert alone.end_turn == "tool_deferred"
    other = ToolUseBlock("t2", "Glob", {"pattern": "*"})
    batch = run_tool_use(block, _tool(ctx, "Glob"), _cc(ctx, siblings=[block, other]))
    assert batch.denied and batch.end_turn is None


def test_pre_tool_hook_context_reaches_the_result(project, config_dir):
    from harnesslab.tools.pipeline import run_tool_use

    ctx = _session(project, config_dir)
    ctx.hook_runner = _Hooks(AggregatedHookResult(additional_contexts=["remember the style guide"]))
    out = run_tool_use(ToolUseBlock("t1", "Glob", {"pattern": "*"}), _tool(ctx, "Glob"), _cc(ctx))
    assert "remember the style guide" in out.result.tool_results()[0].text()


# (e)/(l) prompt builder: agent prompt, fork reuse, auto-mode guidance
def test_prompt_builder_uses_agent_prompt_fork_bytes_and_auto_guidance(project, config_dir):
    from harnesslab.context.prompt import SystemPromptBuilder
    from harnesslab.permissions.classifier import AUTO_MODE_AGENT_GUIDANCE

    ctx = _session(project, config_dir, permission_mode=PermissionMode.AUTO)
    text = " ".join(b.text for b in SystemPromptBuilder().build(ctx, ctx.tools))
    assert AUTO_MODE_AGENT_GUIDANCE in text

    child = ctx.child(agent_id="a1", agent_type="Explore")
    child.extra["agent_system_prompt"] = "You explore code.\n\nNotes for this run: be brief."
    text = " ".join(b.text for b in SystemPromptBuilder().build(child, child.tools))
    assert "You explore code." in text and "# Notes for sub-agents" not in text

    fork = ctx.child(agent_id="f1", agent_type="fork", is_fork=True)
    fork.extra["rendered_system_prompt"] = [SystemBlock("exact parent bytes")]
    assert [b.text for b in SystemPromptBuilder().build(fork, fork.tools)] == ["exact parent bytes"]


def test_omit_claude_md_for_read_only_agents(project, config_dir):
    from harnesslab.context.attachments import instructions_producer
    from harnesslab.interfaces import TurnInfo

    (project / "CLAUDE.md").write_text("project rules")
    ctx = _session(project, config_dir)
    assert instructions_producer(ctx, TurnInfo(items=[], turn_count=0, is_first_turn=True))
    child = ctx.child(agent_id="a1", agent_type="Explore")
    child.extra["omit_claude_md"] = True
    assert instructions_producer(child, TurnInfo(items=[], turn_count=0, is_first_turn=True)) == []


# (g) a finished task is notified exactly once
def test_fallback_notifier_skips_self_notified_and_killed(project, config_dir):
    ctx = _session(project, config_dir)
    t1 = ctx.tasks.register("shell", "plain")
    ctx.tasks.finish(t1.id, "completed")
    t2 = ctx.tasks.register("agent", "self-reporting")
    t2.meta["notified"] = True
    ctx.tasks.finish(t2.id, "completed")
    t3 = ctx.tasks.register("shell", "stopped")
    ctx.tasks.finish(t3.id, "killed")
    drained = ctx.notifications.drain()
    assert len(drained) == 1 and t1.id in drained[0].text()


# (k) the shell sources CLAUDE_ENV_FILE files written by hooks
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_shell_sources_hook_env_files(project, config_dir):
    from harnesslab.tools.pipeline import run_tool_use

    env_file = project / "hook-env.sh"
    env_file.write_text("export HARNESSLAB_FROM_HOOK=yes\n")
    ctx = _session(project, config_dir, permission_mode=PermissionMode.BYPASS)
    ctx.extra["hook_env_files"] = [str(env_file), str(project / "missing.sh")]
    out = run_tool_use(ToolUseBlock("t1", "Bash", {"command": "echo $HARNESSLAB_FROM_HOOK"}), _tool(ctx, "Bash"), _cc(ctx))
    assert "yes" in out.result.tool_results()[0].text()


# context layers: applied after a batch, expired at the next user message
def test_context_layers_apply_and_expire(project, config_dir):
    from harnesslab.loop.context_layers import apply_layers, expire_layers

    ctx = _session(project, config_dir)
    before = list(ctx.permissions.rules)
    apply_layers(ctx, [ContextLayer("allowed_tools", ["Bash(git status:*)"]), ContextLayer("effort", "low")])
    assert any(r.tool_name == "Bash" and r.behavior == "allow" for r in ctx.permissions.rules)
    assert ctx.extra["turn_effort"] == "low"
    expire_layers(ctx)
    assert ctx.permissions.rules == before and ctx.extra["turn_effort"] is None


# WP5 -> WP3: workflow agents' bash command clamp (06 R37)
def test_bash_command_clamp_denies_other_commands(project, config_dir):
    from harnesslab.permissions.pipeline import decide

    ctx = _session(project, config_dir, permission_mode=PermissionMode.BYPASS)
    ctx.extra["bash_command_clamp"] = ["Bash(git status:*)"]
    bash = _tool(ctx, "Bash")
    assert decide(bash, {"command": "git status --short"}, _cc(ctx)).behavior == "allow"
    assert decide(bash, {"command": "curl example.com"}, _cc(ctx)).behavior == "deny"
