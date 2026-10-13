"""WP1 loop tests: transitions, terminal reasons, tools, hooks, abort, budget (01 R1-R12, R51-R54)."""

from __future__ import annotations

from wp1_fakes import FakeExecutor, FakeHooks, drive, make_deps, messages

from harnesslab.agents.notifications import NotificationQueue
from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.loop.query import over_max_turns, run_query
from harnesslab.loop.state import LoopState, QueryParams
from harnesslab.model.scripted import call, say, turn
from harnesslab.types import (
    AttachmentEvent,
    Message,
    MessageEvent,
    StreamRequestStart,
    SystemNoticeEvent,
    TerminalReason,
    TombstoneEvent,
    ToolResultBlock,
    TransitionReason,
    TurnEndEvent,
    Usage,
)


def test_text_only_turn_completes(ctx, scripted):
    scripted.add(say("hello there"))
    events, reason = drive([Message.user("hi")], ctx)
    assert reason == TerminalReason.COMPLETED
    assert isinstance(events[0], StreamRequestStart)
    assert isinstance(events[-1], TurnEndEvent) and events[-1].reason == TerminalReason.COMPLETED
    assert [m.text() for m in messages(events)] == ["hello there"]
    assert ctx.extra["loop_items"][-1].text() == "hello there"


def test_tool_round_results_in_order_then_second_round(ctx, scripted):
    scripted.add(
        turn("two reads", call("Read", {"p": 1}, id="a"), call("Read", {"p": 2}, id="b")),
        say("done"),
    )
    deps = make_deps(lambda: FakeExecutor({"Read": lambda i: f"content {i['p']}"}))
    events, reason = drive([Message.user("go")], ctx, deps=deps)
    assert reason == TerminalReason.COMPLETED
    rows = messages(events)
    assert rows[0].role == "assistant" and [b.id for b in rows[0].tool_uses()] == ["a", "b"]
    results = [b for m in rows[1:3] for b in m.tool_results()]
    assert [r.tool_use_id for r in results] == ["a", "b"]  # original order
    assert rows[-1].text() == "done"
    # the second request carried both results right after the tool calls
    second = scripted.requests[1].messages
    assert [b["tool_use_id"] for b in second[-1]["content"] if b["type"] == "tool_result"] == ["a", "b"]
    assert sum(isinstance(e, StreamRequestStart) for e in events) == 2


def test_transitions_recorded_and_guards_reset_on_next_turn():
    s = LoopState(items=[])
    s.guards.output_limit_recoveries = 2
    s.guards.malformed_tool_retried = True
    kept = s.next(TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY)
    assert kept.transition == TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY
    assert kept.guards.output_limit_recoveries == 2 and kept.turn_count == 0
    fresh = kept.next(TransitionReason.NEXT_TURN)
    assert fresh.guards.output_limit_recoveries == 0 and not fresh.guards.malformed_tool_retried  # 01 R3
    assert fresh.turn_count == 1 and fresh.trace.depth == 2


def test_max_turns_attachment_and_reason(ctx, scripted):
    scripted.add(turn(call("Read", {}, id="a")), turn(call("Read", {}, id="b")))
    deps = make_deps(lambda: FakeExecutor({"Read": lambda i: "x"}))
    events, reason = drive([Message.user("go")], ctx, QueryParams(max_turns=1), deps)
    assert reason == TerminalReason.MAX_TURNS
    att = [e.attachment for e in events if isinstance(e, AttachmentEvent)]
    assert att and att[-1].type == "max_turns_reached"
    assert att[-1].payload == {"max_turns": 1, "turn_count": 1}
    assert len(scripted.requests) == 1  # checked after results, before a second round (01 R5)
    assert over_max_turns(LoopState([], turn_count=0), QueryParams(max_turns=2)) is False
    assert over_max_turns(LoopState([], turn_count=1), QueryParams(max_turns=2)) is True
    assert over_max_turns(LoopState([], turn_count=50), QueryParams()) is False


def test_stop_hook_blocking_continues_then_completes(ctx, scripted):
    scripted.add(say("first"), say("second"))
    seen = []

    def stop(payload):
        seen.append(payload["stop_hook_active"])
        return AggregatedHookResult(blocking_errors=["run the tests"]) if len(seen) == 1 else AggregatedHookResult()

    hooks = FakeHooks({HookEvent.STOP: stop})
    events, reason = drive([Message.user("go")], ctx, deps=make_deps(hooks=hooks))
    assert reason == TerminalReason.COMPLETED
    assert seen == [False, True]  # stop_hook_active once a previous hook blocked (01 R6)
    feedback = [m for m in messages(events) if m.is_meta]
    assert feedback and "run the tests" in feedback[0].text()
    assert "run the tests" in str(scripted.requests[1].messages[-1])


def test_stop_hook_cap_ends_turn_with_warning(ctx, scripted):
    scripted.add(*[say(f"r{i}") for i in range(10)])
    hooks = FakeHooks({HookEvent.STOP: lambda p: AggregatedHookResult(blocking_errors=["again"])})
    params = QueryParams(stop_hook_block_cap=8)
    events, reason = drive([Message.user("go")], ctx, params, make_deps(hooks=hooks))
    assert reason == TerminalReason.COMPLETED
    assert len(scripted.requests) == 8
    warn = [e for e in events if isinstance(e, SystemNoticeEvent) and e.subtype == "stop_hook_cap"]
    assert warn and "stop_hook_active" in warn[0].text


def test_abort_during_stream(ctx, scripted):
    scripted.add(turn("partial text that streams slowly", call("Read", {}, id="a"), say("x").blocks[0]))
    gen = run_query([Message.user("go")], ctx, QueryParams(), make_deps())
    events = []
    for ev in gen:
        events.append(ev)
        if len(events) == 4:  # mid-stream
            ctx.abort.abort("user")
    assert events[-1].reason == TerminalReason.ABORTED_STREAMING
    assert any(isinstance(e, TombstoneEvent) for e in events)
    assert messages(events)[-1].text().startswith("[The user interrupted the response")


def test_abort_during_stream_keeps_partial_for_sdk(ctx, scripted):
    scripted.add(say("a long partial answer " * 4))
    gen = run_query([Message.user("go")], ctx, QueryParams(keep_partial_on_abort=True), make_deps())
    events = []
    for ev in gen:
        events.append(ev)
        if len(events) == 5:
            ctx.abort.abort("user")
    rows = messages(events)
    assert rows[0].role == "assistant" and rows[0].aborted_mid_stream and rows[0].text()  # 01 R53
    assert events[-1].reason == TerminalReason.ABORTED_STREAMING


def test_abort_during_tools_reason(ctx, scripted):
    scripted.add(turn(call("Slow", {}, id="a")))
    ex = FakeExecutor({"Slow": lambda i: "never"}, lazy=True)

    def drain_all():
        ctx.abort.abort("user")  # user presses Esc while the tool runs
        return iter(())

    ex.drain_all = drain_all
    events, reason = drive([Message.user("go")], ctx, deps=make_deps(lambda: ex))
    assert reason == TerminalReason.ABORTED_TOOLS
    rows = messages(events)
    synth = [b for m in rows for b in m.tool_results()]
    assert synth and synth[0].is_error and "Interrupted" in synth[0].text()  # 01 R51/R52
    assert "while tools were running" in rows[-1].text()


def test_mid_turn_input_folded_between_rounds(ctx, scripted):
    def tool(i):
        ctx.pending_user_inputs.append("also check the README")
        return "ok"

    scripted.add(turn(call("Read", {}, id="a")), say("done"))
    events, reason = drive([Message.user("go")], ctx, deps=make_deps(lambda: FakeExecutor({"Read": tool})))
    assert reason == TerminalReason.COMPLETED
    att = [e.attachment for e in events if isinstance(e, AttachmentEvent)]
    assert att[0].type == "midturn_user_input" and att[0].payload["texts"] == ["also check the README"]
    sent = str(scripted.requests[1].messages[-1])
    assert "also check the README" in sent and "system-reminder" in sent  # 01 R54, 02 R30
    assert ctx.pending_user_inputs == []


def test_notifications_drained_into_next_round(ctx, scripted):
    ctx.notifications = NotificationQueue()
    ctx.notifications.notify("t1", "completed", "background job done")
    scripted.add(say("ok"))
    events, _ = drive([Message.user("go")], ctx)
    assert "background job done" in str(scripted.requests[0].messages)


def test_budget_exceeded(ctx, scripted):
    big = Usage(input_tokens=1_000_000, output_tokens=0)
    scripted.add(turn(call("Read", {}, id="a"), usage=big), say("never sent"))
    params = QueryParams(max_budget_usd=1.0)
    events, reason = drive([Message.user("go")], ctx, params, make_deps(lambda: FakeExecutor({"Read": lambda i: "x"})))
    assert reason == TerminalReason.BUDGET_EXCEEDED  # 01 R8 / G14
    assert ctx.cost_usd >= 1.0 and len(scripted.requests) == 1


def test_unknown_tool_result_and_tool_end_turn(ctx, scripted):
    scripted.add(turn(call("Defer", {}, id="a")))
    deps = make_deps(lambda: FakeExecutor({"Defer": lambda i: "queued"}, end_turn="tool_deferred"))
    _, reason = drive([Message.user("go")], ctx, deps=deps)
    assert reason == TerminalReason.TOOL_DEFERRED  # 01 R7


def test_post_tool_batch_hook_once_per_batch(ctx, scripted):
    scripted.add(turn(call("Read", {}, id="a"), call("Read", {}, id="b")), say("done"))
    hooks = FakeHooks({HookEvent.POST_TOOL_BATCH: lambda p: AggregatedHookResult(additional_contexts=["lint is clean"])})
    events, reason = drive([Message.user("go")], ctx, deps=make_deps(lambda: FakeExecutor({"Read": lambda i: "x"}), hooks=hooks))
    batch_calls = [c for c in hooks.calls if c[0] == HookEvent.POST_TOOL_BATCH]
    assert len(batch_calls) == 1 and len(batch_calls[0][1]["tool_calls"]) == 2
    assert "lint is clean" in str(scripted.requests[1].messages)


def test_transcript_receives_every_row(ctx, scripted):
    class Store:
        def __init__(self):
            self.rows = []

        def append(self, item):
            self.rows.append(item)

    ctx.transcript = Store()
    scripted.add(turn(call("Read", {}, id="a")), say("done"))
    events, _ = drive([Message.user("go")], ctx, deps=make_deps(lambda: FakeExecutor({"Read": lambda i: "x"})))
    roles = [r.role for r in ctx.transcript.rows]
    assert roles == ["assistant", "user", "assistant"]  # tool_use precedes its result
    assert isinstance(ctx.transcript.rows[1].content[0], ToolResultBlock)
    assert len(messages(events)) == 3
