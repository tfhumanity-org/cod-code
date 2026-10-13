"""WP5 - agent definitions, launcher, forks and the Agent/SendMessage/TaskStop tools (spec 06 R1-R29)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from wp5_fakes import NamedTool, RecordingRunner, mini_run_query, names, no_deps

from harnesslab.agents.agent_tool import AgentTool, SendMessageTool, TaskStopTool, validate_agent_name
from harnesslab.agents.definitions import (
    AgentDefinition,
    AgentDefinitionError,
    builtin_agents,
    load_agents,
    parse_agent_file,
    resolve_type,
)
from harnesslab.agents.launcher import (
    FORK_PLACEHOLDER,
    AgentAsyncLaunched,
    AgentCompleted,
    AgentInput,
    LaunchRefused,
    SubagentLauncher,
    async_launch_text,
    build_fork,
    child_permission_mode,
    filter_tools,
    resolve_model,
)
from harnesslab.agents.notifications import NotificationQueue
from harnesslab.agents.tasks import TaskRegistry
from harnesslab.model.base import APIError
from harnesslab.model.scripted import call, fail, say, turn
from harnesslab.permissions.types import PermissionMode, Rule
from harnesslab.tools.base import ToolCallContext
from harnesslab.types import ErrorKind, Message, TerminalReason, TextBlock, ToolResultBlock, ToolUseBlock, TurnEndEvent


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def launcher(**kw) -> SubagentLauncher:
    return SubagentLauncher(run_query=kw.pop("run_query", mini_run_query), make_deps=no_deps, **kw)


def with_bg(ctx):
    ctx.tasks = TaskRegistry()
    ctx.notifications = NotificationQueue()
    return ctx


def tcx(ctx, tid="toolu_1"):
    return ToolCallContext(ctx, tid, ctx.abort.signal)


# ------------------------------------------------------------------ definitions (06 §2.5, R6, R25)


def test_parse_agent_file_all_keys(tmp_path):
    f = write(
        tmp_path / "reviewer.md",
        "---\nname: reviewer\ndescription: Reviews code.\\nCarefully.\ntools: Read, Grep, Agent(Explore, Plan)\n"
        "disallowedTools: [mcp__github]\nmodel: sonnet\npermissionMode: plan\nmaxTurns: 7\nmemory: project\n"
        "skills: [lint]\nbackground: true\nomitClaudeMd: true\n---\nYou review code.\n",
    )
    a = parse_agent_file(f, "projectSettings")
    assert a.name == "reviewer" and a.description == "Reviews code.\nCarefully."
    assert a.tools == ["Read", "Grep", "Agent", "Write", "Edit"]  # memory adds Read/Write/Edit
    assert a.allowed_agent_types == ["Explore", "Plan"]
    assert a.disallowed_tools == ["mcp__github"]
    assert (a.model, a.permission_mode, a.max_turns) == ("sonnet", PermissionMode.PLAN, 7)
    assert a.skills == ["lint"] and a.background and a.omit_claude_md
    assert a.prompt == "You review code." and a.source == "projectSettings"


@pytest.mark.parametrize(
    "front, needle",
    [
        ("name: a:b\ndescription: x", "must not contain ':'"),
        ("name: -a\ndescription: x", "must not start"),
        ("name: " + "x" * 65 + "\ndescription: x", "longer than"),
        ("name: a\n", "description"),
        ("name: a\ndescription: x\nPreToolUse: foo", "under 'hooks'"),
        ("name: a\ndescription: x\nmaxTurns: 0", "positive"),
        ("name: a\ndescription: x\npermissionMode: godmode", "permissionMode"),
    ],
)
def test_parse_agent_file_rejections(tmp_path, front, needle):
    f = write(tmp_path / "a.md", f"---\n{front}\n---\nbody")
    with pytest.raises(AgentDefinitionError, match=needle):
        parse_agent_file(f, "userSettings")


def test_builtins_and_precedence(tmp_path):
    b = {a.name: a for a in builtin_agents()}
    assert {"general-purpose", "Explore", "Plan", "web-fetch"} <= set(b)
    assert not b["Explore"].continuable and not b["Plan"].continuable
    assert "Edit" in b["Explore"].disallowed_tools and b["web-fetch"].max_turns == 15
    user = write(tmp_path / "user" / "x.md", "---\nname: helper\ndescription: user one\n---\nU")
    proj = write(tmp_path / "proj" / "x.md", "---\nname: helper\ndescription: project one\n---\nP")
    write(tmp_path / "user" / "e.md", "---\nname: Explore\ndescription: my explore\n---\nE")
    agents = load_agents({"userSettings": [user.parent], "projectSettings": [proj.parent]})
    assert agents["helper"].description == "project one"  # project > user
    assert agents["Explore"].description == "my explore"  # user > built-in


def test_resolve_type_errors_list_candidates():
    agents = {a.name: a for a in builtin_agents()}
    assert resolve_type("General_Purpose", agents, denied=set(), allowed=None).name == "general-purpose"
    with pytest.raises(AgentDefinitionError, match="Available agent types: .*Explore"):
        resolve_type("nope", agents, denied=set(), allowed=None)
    with pytest.raises(AgentDefinitionError, match="denied by a permission rule"):
        resolve_type("explore", agents, denied={"Explore"}, allowed=None)
    with pytest.raises(AgentDefinitionError, match="may only spawn"):
        resolve_type("Plan", agents, denied=set(), allowed=["Explore"])
    agents["general.purpose"] = AgentDefinition("general.purpose", "dup")
    with pytest.raises(AgentDefinitionError, match="ambiguous"):
        resolve_type("GENERAL-PURPOSE", agents, denied=set(), allowed=None)


# ------------------------------------------------------------------ tool filtering (06 §4.2, R17)

POOL = ["Read", "Edit", "Write", "Bash", "Agent", "ExitPlanMode", "AskUserQuestion", "Workflow", "mcp__srv__x", "mcp__other__y", "WebFetch", "TaskCreate", "SendMessage"]


def pool():
    return [NamedTool(n) for n in POOL]


def test_filter_never_set_and_plan_exit():
    out = names(filter_tools(pool(), AgentDefinition("a", "d"), is_async=False, is_teammate=False, depth=1))
    assert "AskUserQuestion" not in out and "Workflow" not in out and "ExitPlanMode" not in out
    assert "Agent" in out
    plan = names(filter_tools(pool(), AgentDefinition("a", "d", permission_mode=PermissionMode.PLAN), is_async=False, is_teammate=False, depth=1))
    assert "ExitPlanMode" in plan


def test_filter_async_allowlist_and_teammate_extras_and_depth():
    out = names(filter_tools(pool(), AgentDefinition("a", "d"), is_async=True, is_teammate=False, depth=1))
    assert out == ["Read", "Edit", "Write", "Bash", "mcp__srv__x", "mcp__other__y", "WebFetch", "SendMessage"]
    team = names(filter_tools(pool(), AgentDefinition("a", "d"), is_async=True, is_teammate=True, depth=1))
    assert "TaskCreate" in team
    capped = names(filter_tools(pool(), AgentDefinition("a", "d"), is_async=False, is_teammate=False, depth=3))
    assert "Agent" not in capped


def test_filter_definition_allow_deny_and_mcp_family():
    a = AgentDefinition("a", "d", tools=["Read", "mcp__srv"], disallowed_tools=[])
    assert names(filter_tools(pool(), a, is_async=False, is_teammate=False, depth=1)) == ["Read", "mcp__srv__x"]
    b = AgentDefinition("b", "d", disallowed_tools=["mcp__other__*", "Bash"])
    out = names(filter_tools(pool(), b, is_async=False, is_teammate=False, depth=1))
    assert "mcp__other__y" not in out and "Bash" not in out and "mcp__srv__x" in out
    tool = NamedTool("Bash2", aliases=("Shell",))
    assert names(filter_tools([tool], AgentDefinition("c", "d", tools=["Shell"]), is_async=False, is_teammate=False, depth=1)) == ["Bash2"]


def test_filter_zero_tools_after_declaration_is_refusal():
    with pytest.raises(LaunchRefused, match="none of them is available"):
        filter_tools(pool(), AgentDefinition("a", "d", tools=["Agent"]), is_async=True, is_teammate=False, depth=1)
    # never declared -> empty pool is fine (no silent widening either way)
    assert filter_tools([], AgentDefinition("g", "d"), is_async=True, is_teammate=False, depth=1) == []


# ------------------------------------------------------------------ launch caps (06 R2-R5, §4.1)


def test_depth_cap_refusal(ctx):
    ctx.agent_depth = 3
    with pytest.raises(LaunchRefused, match="nesting limit.*yourself"):
        launcher().launch(ctx, AgentInput("d", "p"))


def test_budget_refusal(ctx):
    ctx.max_budget_usd, ctx.cost_usd = 1.0, 1.5
    with pytest.raises(LaunchRefused, match="budget"):
        launcher().launch(ctx, AgentInput("d", "p"))


def test_concurrency_cap_refusal(ctx, scripted):
    with_bg(ctx)
    ctx.env = {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "1"}
    gate = threading.Event()
    runner = RecordingRunner(block=gate)
    scripted.add(say("first done"))
    L = launcher(run_query=runner)
    first = L.launch(ctx, AgentInput("one", "work"))
    assert isinstance(first, AgentAsyncLaunched)
    with pytest.raises(LaunchRefused, match="do not retry"):
        L.launch(ctx, AgentInput("two", "work"))
    gate.set()
    assert ctx.notifications.wait(5)


def test_deny_rule_on_type(ctx):
    ctx.permissions.rules.append(Rule("Agent", "Explore", "deny"))
    with pytest.raises(LaunchRefused, match="denied"):
        launcher().launch(ctx, AgentInput("d", "p", subagent_type="Explore", run_in_background=False))


def test_spawn_hook_rewrite_rechecked(ctx, scripted):
    ctx.permissions.rules.append(Rule("Agent", "Plan", "deny"))
    ctx.extra["agent_spawn_hook"] = lambda inp: {"input": AgentInput(inp.description, inp.prompt, subagent_type="Plan", run_in_background=False)}
    with pytest.raises(LaunchRefused, match="Dispatch the task directly"):
        launcher().launch(ctx, AgentInput("d", "p", run_in_background=False))
    ctx.extra["agent_spawn_hook"] = lambda inp: {"deny": "not today"}
    with pytest.raises(LaunchRefused, match="not today"):
        launcher().launch(ctx, AgentInput("d", "p", run_in_background=False))


def test_isolation_is_refused_not_ignored(ctx):
    with pytest.raises(LaunchRefused, match="Worktree isolation"):
        launcher().launch(ctx, AgentInput("d", "p", isolation="worktree"))


def test_model_precedence_and_mode_escalation():
    gp = AgentDefinition("general-purpose", "d", model="haiku")
    assert resolve_model("opus", gp, {}, "parent") == "opus"
    assert resolve_model(None, gp, {}, "parent") == "haiku"
    assert resolve_model(None, AgentDefinition("x", "d"), {"CLAUDE_CODE_SUBAGENT_MODEL": "envm"}, "parent") == "envm"
    assert resolve_model("opus", gp, {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}, "parent") == "haiku"
    assert resolve_model(None, AgentDefinition("Explore", "d"), {}, "claude-opus-x") == "sonnet"
    assert resolve_model(None, AgentDefinition("x", "d"), {}, "claude-opus-x") == "claude-opus-x"
    D, B, A = PermissionMode.DEFAULT, PermissionMode.BYPASS, PermissionMode.ACCEPT_EDITS
    assert child_permission_mode(D, B, bypass_available=False) == D
    assert child_permission_mode(D, A, bypass_available=False) == D
    assert child_permission_mode(B, A, bypass_available=False) == A
    assert child_permission_mode(D, PermissionMode.PLAN, bypass_available=False) == PermissionMode.PLAN


# ------------------------------------------------------------------ sync / async (06 R1, R12, R13, R19, R21)


def test_sync_completed_with_trailer_and_handle(ctx, scripted):
    scripted.add(turn("Reading", call("Read", {"file_path": "/x"})), say("All done: found 3 bugs"))
    ctx.tools = [NamedTool("Read")]
    res = launcher().launch(ctx, AgentInput("find bugs", "look", run_in_background=False))
    assert isinstance(res, AgentCompleted)
    assert res.content == "All done: found 3 bugs" and res.tool_uses == 1 and res.agent_id
    scripted.add(say("explored"))
    out = AgentTool(launcher()).call({"description": "x", "prompt": "y", "run_in_background": False, "subagent_type": "Explore"}, tcx(ctx))
    text = out.blocks[0].text
    assert "explored" in text and "<usage>total_tokens:" in text and "tool_uses: 0" in text
    assert "SendMessage" not in text and out.data["canContinueAgent"] is False  # Explore is not continuable


def test_child_context_and_prompt_notes(ctx, scripted):
    runner = RecordingRunner()
    scripted.add(say("ok"))
    ctx.tools = [NamedTool("Read"), NamedTool("AskUserQuestion")]
    launcher(run_query=runner).launch(ctx, AgentInput("d", "the task", run_in_background=False))
    items, child, params = runner.calls[0]
    assert child.agent_depth == 1 and child.agent_id and params.is_subagent
    assert names(child.tools) == ["Read"]
    notes = child.extra["agent_system_prompt"]
    assert "absolute paths" in notes and "report" in notes and "consent" in notes
    assert isinstance(items[-1], Message) and items[-1].text() == "the task"


def test_async_default_launch_text_and_notification(ctx, scripted):
    with_bg(ctx)
    scripted.add(say("the answer is 42"))
    out = AgentTool(launcher()).call({"description": "compute", "prompt": "compute it"}, tcx(ctx))
    text = out.blocks[0].text
    aid = out.data["agentId"]
    assert out.data["status"] == "async_launched"
    for needle in (aid, "SendMessage", "do not show it to the user", "know nothing", "Do not read or tail"):
        assert needle in text
    assert ctx.notifications.wait(5)
    note = ctx.notifications.drain()[0].text()
    assert f"<task-id>{aid}</task-id>" in note and "the answer is 42" in note and "<status>completed</status>" in note
    assert ctx.tasks.get(aid).status == "completed"
    assert Path(out.data["outputFile"]).exists()


def test_partial_output_recovered_on_retryable_error(ctx, scripted):
    def runner(items, c, params=None, deps=None):
        yield from ()
        from harnesslab.types import MessageEvent

        yield MessageEvent(Message.assistant("half of the work"))
        raise APIError(ErrorKind.OVERLOADED, "busy")

    res = launcher(run_query=runner).launch(ctx, AgentInput("d", "p", run_in_background=False))
    assert res.partial_recovered and "half of the work" in res.content and "partial output recovered" in res.content


def test_max_turns_note(ctx, scripted):
    def runner(items, c, params=None, deps=None):
        from harnesslab.types import MessageEvent

        yield MessageEvent(Message.assistant("step one"))
        yield TurnEndEvent(TerminalReason.MAX_TURNS, 1)

    res = launcher(run_query=runner).launch(ctx, AgentInput("d", "p", run_in_background=False))
    assert res.stopped_at_turn_limit and "turn limit" in res.content and "SendMessage" in res.content


def test_auto_background_after_threshold(ctx, scripted):
    with_bg(ctx)
    ctx.env = {"CLAUDE_AUTO_BACKGROUND_TASKS": "1"}
    gate = threading.Event()
    scripted.add(say("slow result"))
    L = launcher(run_query=RecordingRunner(block=gate), auto_background_ms=50)
    ctx.agent_id = "parent"  # a subagent caller defaults to sync
    res = L.launch(ctx, AgentInput("slow", "p"))
    assert isinstance(res, AgentAsyncLaunched)
    gate.set()
    assert ctx.notifications.wait(5)
    assert "slow result" in ctx.notifications.drain()[0].text()


# ------------------------------------------------------------------ forks (06 R26-R27, §4.3)


def test_build_fork_is_cache_identical(ctx):
    ctx.env = {"CLAUDE_CODE_FORK_SUBAGENT": "1"}
    ctx.tools = [NamedTool("Read"), NamedTool("Agent")]
    ctx.extra["rendered_system_prompt"] = "SYSTEM BYTES"
    u1 = Message.user("hi")
    a1 = Message.assistant([TextBlock("forking"), ToolUseBlock("t1", "Agent", {}), ToolUseBlock("t2", "Read", {})])
    child, items = build_fork(ctx, [u1, a1], "investigate X", worktree="/wt")
    assert items[0] is u1 and items[1] == a1 and items[1] is not a1
    last = items[-1]
    results = [b for b in last.content if isinstance(b, ToolResultBlock)]
    assert [r.tool_use_id for r in results] == ["t1", "t2"] and all(r.content == FORK_PLACEHOLDER for r in results)
    directive = last.content[-1].text
    assert "reference" in directive and "do not spawn" in directive and "restating" in directive and "/wt" in directive
    assert child.is_fork and child.permissions.mode == PermissionMode.BUBBLE and child.max_turns == 200
    assert child.tools == ctx.tools and child.extra["rendered_system_prompt"] == "SYSTEM BYTES"
    with pytest.raises(LaunchRefused, match="forks cannot fork"):
        build_fork(child, items, "again")


def test_fork_launch_gated_and_async(ctx, scripted):
    with pytest.raises(LaunchRefused, match="not enabled"):
        launcher().launch(ctx, AgentInput("d", "p", subagent_type="fork"))
    with_bg(ctx)
    ctx.env = {"CLAUDE_CODE_FORK_SUBAGENT": "1"}
    scripted.add(say("fork report"))
    res = launcher().launch(ctx, AgentInput("d", "p", subagent_type="fork"), parent_items=[Message.user("x"), Message.assistant("y")])
    assert isinstance(res, AgentAsyncLaunched)
    assert ctx.notifications.wait(5)


# ------------------------------------------------------------------ continuation & stop (06 R28, R29)


def test_sendmessage_resumes_finished_agent(ctx, scripted):
    runner = RecordingRunner()
    L = launcher(run_query=runner)
    ctx.extra["subagent_launcher"] = L
    scripted.add(say("first answer"), say("second answer"))
    first = L.launch(ctx, AgentInput("d", "original task", run_in_background=False))
    out = SendMessageTool().call({"to": first.agent_id, "message": "follow up"}, tcx(ctx))
    assert "second answer" in out.blocks[0].text
    items = runner.calls[1][0]
    texts = [m.text() for m in items if isinstance(m, Message)]
    assert texts[0] == "original task" and "first answer" in texts and texts[-1] == "follow up"


def test_sendmessage_unknown_and_not_continuable(ctx, scripted):
    L = launcher()
    ctx.extra["subagent_launcher"] = L
    assert SendMessageTool().call({"to": "a00000000", "message": "x"}, tcx(ctx)).is_error
    scripted.add(say("explored"))
    r = L.launch(ctx, AgentInput("d", "p", subagent_type="Explore", run_in_background=False))
    assert r.agent_id is None
    rec_id = next(iter(L._records))
    out = SendMessageTool().call({"to": rec_id, "message": "x"}, tcx(ctx))
    assert out.is_error and "cannot be continued" in out.blocks[0].text


def test_sendmessage_to_running_agent_is_mid_turn_input(ctx, scripted):
    with_bg(ctx)
    gate = threading.Event()
    runner = RecordingRunner(block=gate)
    L = launcher(run_query=runner)
    ctx.extra["subagent_launcher"] = L
    scripted.add(say("done"))
    res = L.launch(ctx, AgentInput("d", "p", name="worker"))
    assert runner.started.wait(5)
    out = SendMessageTool().call({"to": "worker", "message": "also do Y"}, tcx(ctx))
    assert "delivered" in out.blocks[0].text
    assert runner.calls[0][1].pending_user_inputs == ["also do Y"]
    gate.set()
    assert ctx.notifications.wait(5)
    assert res.agent_id


def test_taskstop_aliases_and_names(ctx, scripted):
    assert set(TaskStopTool.aliases) == {"KillShell", "KillBash"}
    with_bg(ctx)
    stopped = []
    t = ctx.tasks.register("shell", "sleep forever", name="sleeper", stop=lambda: stopped.append(1))
    out = TaskStopTool().call({"task_id": "sleeper"}, tcx(ctx))
    assert not out.is_error and stopped == [1] and ctx.tasks.get(t.id).status == "killed"
    again = TaskStopTool().call({"task_id": t.id}, tcx(ctx))
    assert again.is_error and "not running" in again.blocks[0].text
    unknown = TaskStopTool().call({"task_id": "zzz"}, tcx(ctx))
    assert unknown.is_error and "No task" in unknown.blocks[0].text


def test_taskstop_running_agent_then_no_notification(ctx, scripted):
    with_bg(ctx)
    gate = threading.Event()
    L = launcher(run_query=RecordingRunner(block=gate))
    ctx.extra["subagent_launcher"] = L
    scripted.add(say("late"))
    res = L.launch(ctx, AgentInput("d", "p"))
    out = TaskStopTool().call({"task_id": res.agent_id}, tcx(ctx))
    assert "continued later" in out.blocks[0].text
    gate.set()
    import time

    time.sleep(0.2)
    assert len(ctx.notifications) == 0  # the stop result already told the model


def test_agent_name_validation():
    assert validate_agent_name("researcher") is None
    assert validate_agent_name("main") and validate_agent_name("a1b2c3d4zz") is None
    assert validate_agent_name("a1b2c3d4e") and validate_agent_name("9lives")


def test_async_launch_text_function():
    t = async_launch_text(AgentAsyncLaunched("a12345678", "/tmp/out.jsonl"))
    assert "a12345678" in t and "/tmp/out.jsonl" in t


def test_required_mcp_servers_wait_then_refuse(ctx, scripted):
    agents = {"needs-gh": AgentDefinition("needs-gh", "d", required_mcp=["git hub"])}
    L = SubagentLauncher(agents, run_query=mini_run_query, make_deps=no_deps, required_mcp_wait_s=0)
    with pytest.raises(LaunchRefused, match="git hub"):
        L.launch(ctx, AgentInput("d", "p", subagent_type="needs-gh", run_in_background=False))
    ctx.tools = [NamedTool("mcp__git_hub__list")]
    scripted.add(say("ok"))
    assert L.launch(ctx, AgentInput("d", "p", subagent_type="needs-gh", run_in_background=False)).content == "ok"


def test_subagent_start_hook_blocks_or_adds_context(ctx, scripted):
    from harnesslab.hooks.types import AggregatedHookResult, HookEvent

    class Hooks:
        def __init__(self, result):
            self.result, self.events = result, []

        def run(self, event, payload, c):
            self.events.append(event)
            return self.result if event == HookEvent.SUBAGENT_START else AggregatedHookResult()

    ctx.hook_runner = Hooks(AggregatedHookResult(blocking_errors=["no agents on Fridays"]))
    with pytest.raises(LaunchRefused, match="Fridays"):
        launcher().launch(ctx, AgentInput("d", "p", run_in_background=False))
    ctx.hook_runner = Hooks(AggregatedHookResult(additional_contexts=["repo uses tabs"]))
    runner = RecordingRunner()
    scripted.add(say("ok"))
    launcher(run_query=runner).launch(ctx, AgentInput("d", "p", run_in_background=False))
    items = runner.calls[0][0]
    assert any(isinstance(m, Message) and m.is_meta and m.text() == "repo uses tabs" for m in items)
    assert ctx.hook_runner.events == [HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP]


def test_async_child_is_headless_unless_bubbling(ctx, scripted):
    with_bg(ctx)
    runner = RecordingRunner()
    scripted.add(say("ok"))
    launcher(run_query=runner).launch(ctx, AgentInput("d", "p"))
    assert ctx.notifications.wait(5)
    assert runner.calls[0][1].permissions.headless
