"""WP1 session.Engine and cli tests with fake collaborators (01#T12, 04 R33, 05 R46, 07 R30)."""

from __future__ import annotations

import io

from wp1_fakes import FakeExecutor, FakeHooks, make_deps

from harnesslab import cli
from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.model.base import ModelRequest
from harnesslab.model.scripted import call, say, turn
from harnesslab.permissions.types import PermissionPromptRequest, Decision
from harnesslab.session import Engine, checkpointing_enabled
from harnesslab.types import MessageEvent, SystemNoticeEvent, TerminalReason, TurnEndEvent


class FakeHistory:
    def __init__(self):
        self.snapshots = []

    def snapshot(self, message_id):
        self.snapshots.append(message_id)


def test_engine_submit_runs_turn_and_keeps_history(ctx, scripted):
    scripted.add(say("one"), say("two"))
    ctx.file_history = FakeHistory()
    eng = Engine(ctx, deps=make_deps())
    events = eng.run("first")
    assert isinstance(events[0], MessageEvent) and events[0].message.text() == "first"
    assert eng.last_reason == TerminalReason.COMPLETED
    assert ctx.file_history.snapshots == [events[0].message.uuid]  # 05 R46: one per prompt
    eng.run("second")
    assert [m.text() for m in eng.items] == ["first", "one", "second", "two"]
    assert "one" in str(scripted.requests[1].messages)  # history carried into turn 2


def test_user_prompt_submit_block_erases_prompt(ctx, scripted):
    ctx.hook_runner = FakeHooks({HookEvent.USER_PROMPT_SUBMIT: lambda p: AggregatedHookResult(blocking_errors=["no secrets"])})
    eng = Engine(ctx, deps=make_deps())
    events = eng.run("my password is hunter2")
    assert eng.items == [] and eng.last_reason is None  # 04 R33
    assert isinstance(events[0], SystemNoticeEvent) and "no secrets" in events[0].text
    assert scripted.requests == []


def test_user_prompt_submit_additional_context(ctx, scripted):
    ctx.hook_runner = FakeHooks({HookEvent.USER_PROMPT_SUBMIT: lambda p: AggregatedHookResult(additional_contexts=["branch: main"])})
    scripted.add(say("ok"))
    Engine(ctx, deps=make_deps()).run("hi")
    assert "branch: main" in str(scripted.requests[0].messages)


def test_engine_interrupt_aborts_turn(ctx, scripted):
    scripted.add(turn("working " * 20, call("Read", {}, id="a")))
    eng = Engine(ctx, deps=make_deps(lambda: FakeExecutor({"Read": lambda i: "x"})))
    seen = []
    for ev in eng.submit("go"):
        seen.append(ev)
        if len(seen) == 4:
            eng.interrupt()
    assert eng.last_reason == TerminalReason.ABORTED_STREAMING
    assert isinstance(seen[-1], TurnEndEvent)


def test_checkpointing_switches(ctx):
    assert checkpointing_enabled(ctx)
    ctx.env = {"CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1"}
    assert not checkpointing_enabled(ctx)
    ctx.env, ctx.is_sdk = {}, True
    assert not checkpointing_enabled(ctx)
    ctx.env = {"CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "1"}
    assert checkpointing_enabled(ctx)


def test_demo_model_replies():
    m = cli.demo_model()
    req = ModelRequest(model="m", messages=[{"role": "user", "content": [{"type": "text", "text": "read /tmp/x"}]}])
    events = list(m.stream(req))
    assert any(getattr(e, "block", None) is not None and getattr(e.block, "name", "") == "Read" for e in events)
    req2 = ModelRequest(model="m", messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "file body"}]}])
    text = "".join(e.value for e in m.stream(req2) if getattr(e, "delta_type", "") == "text")
    assert "file body" in text


def test_terminal_prompter_answers():
    req = PermissionPromptRequest("Bash", {"command": "ls"}, "t1", Decision.ask())
    out = io.StringIO()
    assert cli.terminal_prompter(io.StringIO("y\n"), out)(req).behavior == "allow"
    assert cli.terminal_prompter(io.StringIO("n\n"), out)(req).behavior == "deny"
    always = cli.terminal_prompter(io.StringIO("a\n"), out)(req)
    assert always.behavior == "allow" and always.updated_permissions[0].rules[0].tool_name == "Bash"
    assert "Allow Bash" in out.getvalue()


def test_cli_main_one_exchange(ctx, monkeypatch):
    model = cli.demo_model()
    ctx.model, ctx.model_id = model, model.name
    built = {}

    def fake_build(args, prompter):
        built["args"] = args
        return Engine(ctx, deps=make_deps())

    monkeypatch.setattr(cli, "build_engine", fake_build)
    out = io.StringIO()
    rc = cli.main(["--max-turns", "3", "--permission-mode", "acceptEdits", "--no-persist"], stdin=io.StringIO("hello\nexit\n"), stdout=out)
    assert rc == 0
    text = out.getvalue()
    assert "You said: hello" in text and "[turn ended: completed]" in text
    assert built["args"].max_turns == 3 and built["args"].permission_mode == "acceptEdits"
