"""WP1 recovery ladder tests (01 R26-R32)."""

from __future__ import annotations

from wp1_fakes import StubCompactor, drive, make_deps, messages, no_sleep

from harnesslab.loop.recovery import cut_kind, recovery_ladder
from harnesslab.loop.state import LoopState, QueryParams
from harnesslab.model.base import APIError
from harnesslab.model.scripted import ScriptedTurn, call, fail, say, turn
from harnesslab.types import (
    ErrorKind,
    Message,
    TerminalReason,
    TextBlock,
    ThinkingBlock,
    TombstoneEvent,
    ToolUseBlock,
    TransitionReason,
)


def _ptl() -> APIError:
    return APIError(ErrorKind.PROMPT_TOO_LONG, "prompt is too long", status=400, actual_tokens=210_000, limit_tokens=200_000)


def test_prompt_too_long_reactive_compaction_once(ctx, scripted):
    scripted.add(fail(_ptl()), say("fits now"))
    comp = StubCompactor()
    events, reason = drive([Message.user("big")], ctx, deps=make_deps(compactor=comp))
    assert reason == TerminalReason.COMPLETED
    assert comp.reactive_calls == [10_000]  # parsed gap (01 R26)
    # the withheld error row was never shown
    assert not any(m.is_api_error for m in messages(events))
    assert "Summary of the earlier" in str(scripted.requests[1].messages)


def test_prompt_too_long_without_compaction_surfaces(ctx, scripted):
    scripted.add(fail(_ptl()))
    events, reason = drive([Message.user("big")], ctx, deps=make_deps(compactor=StubCompactor(reactive_ok=False)))
    assert reason == TerminalReason.PROMPT_TOO_LONG
    assert messages(events)[-1].is_api_error


def test_prompt_too_long_second_time_terminal(ctx, scripted):
    scripted.add(fail(_ptl()), fail(_ptl()))
    comp = StubCompactor()
    _, reason = drive([Message.user("big")], ctx, deps=make_deps(compactor=comp))
    assert reason == TerminalReason.PROMPT_TOO_LONG and len(comp.reactive_calls) == 1


def test_image_error_path(ctx, scripted):
    scripted.add(fail(APIError(ErrorKind.IMAGE_TOO_LARGE, "image too large", status=400)))
    _, reason = drive([Message.user("img")], ctx, deps=make_deps())
    assert reason == TerminalReason.IMAGE_ERROR


def test_thrash_breaker(ctx, scripted):
    scripted.add(fail(_ptl()))
    events, reason = drive([Message.user("x")], ctx, deps=make_deps(compactor=StubCompactor(thrash=True)))
    assert reason == TerminalReason.RAPID_REFILL_BREAKER  # 01 R27
    assert "thrashing" in messages(events)[-1].text()


def test_max_tokens_three_nudges_then_error(ctx, scripted):
    scripted.add(*[say(f"part {i}", stop_reason="max_tokens") for i in range(4)])
    events, reason = drive([Message.user("long essay")], ctx, deps=make_deps())
    assert reason == TerminalReason.COMPLETED
    assert len(scripted.requests) == 4  # original + 3 recoveries (01 R28)
    nudges = [m for m in messages(events) if m.is_meta]
    assert len(nudges) == 3
    first = nudges[0].text().lower()
    assert "no apology" in first and "smaller pieces" in first and "code fence" in first  # text-cut variant
    err = messages(events)[-1]
    assert err.is_api_error and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in err.text()
    # nudges sent as hidden reminders, the cut text kept in history
    assert "part 0" in str(scripted.requests[1].messages)


def test_max_tokens_reasoning_cut_variant(ctx):
    cut = Message.assistant([ThinkingBlock("hmm " * 50)], stop_reason="max_tokens")
    assert cut_kind(cut) == "reasoning"
    out = recovery_ladder(LoopState([]), cut, ctx, QueryParams(), None)
    assert out.reason == TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY
    assert "discarded" in out.extra[0].text() and out.keep_assistant is False
    assert cut_kind(Message.assistant([TextBlock("| a | b")], stop_reason="max_tokens")) == "text"
    assert cut_kind(Message.assistant([TextBlock("x"), ToolUseBlock("t", "Read")], stop_reason="max_tokens")) == "generic"


def test_truncated_stream_nudged_for_subagent(ctx, scripted):
    no_sleep(ctx)
    scripted.add(
        ScriptedTurn([TextBlock("half an ans" * 10)], error=APIError(ErrorKind.TRUNCATED, "dropped"), error_after_events=4),
        say("complete answer"),
    )
    params = QueryParams(is_subagent=True, is_interactive=False)
    events, reason = drive([Message.user("q")], ctx, params, make_deps())
    assert reason == TerminalReason.COMPLETED
    nudge = [m for m in messages(events) if m.is_meta]
    assert nudge and "from the start" in nudge[0].text()  # subagent rewrite variant (01 R29)


def test_truncated_stream_interactive_surfaces(ctx, scripted):
    scripted.add(ScriptedTurn([TextBlock("half " * 20)], error=APIError(ErrorKind.TRUNCATED, "dropped"), error_after_events=4))
    _, reason = drive([Message.user("q")], ctx, QueryParams(is_interactive=True), make_deps())
    assert reason == TerminalReason.API_ERROR


def test_malformed_tool_use_retried_once_then_exhausted(ctx, scripted):
    bad = lambda: turn(call("Read", id="t1"), raw_tool_json={"t1": "{bad json"})  # noqa: E731
    scripted.add(bad(), turn(call("Read", id="t1"), raw_tool_json={"t1": "{bad"}))
    events, reason = drive([Message.user("go")], ctx, deps=make_deps())
    assert reason == TerminalReason.MALFORMED_TOOL_USE_EXHAUSTED  # 01 R30
    assert sum(isinstance(e, TombstoneEvent) for e in events) >= 1
    assert "could not be parsed" in str(scripted.requests[1].messages)


def test_malformed_then_good_tool_use(ctx, scripted):
    from wp1_fakes import FakeExecutor

    scripted.add(turn(call("Read", id="t1"), raw_tool_json={"t1": "{bad"}), turn(call("Read", {"a": 1}, id="t2")), say("ok"))
    events, reason = drive([Message.user("go")], ctx, deps=make_deps(lambda: FakeExecutor({"Read": lambda i: "x"})))
    assert reason == TerminalReason.COMPLETED
    # the malformed reply was retracted, so it is not in the next request
    assert "t1" not in str(scripted.requests[1].messages)


def test_thinking_only_nudged_once(ctx, scripted):
    scripted.add(ScriptedTurn([ThinkingBlock("pondering")]), ScriptedTurn([ThinkingBlock("again")]))
    events, reason = drive([Message.user("q")], ctx, deps=make_deps())
    assert reason == TerminalReason.COMPLETED
    assert len(scripted.requests) == 2  # one nudge only (01 R31)
    assert "visible response" in str(scripted.requests[1].messages)


def test_classifier_stop_note(ctx):
    cut = Message.assistant("some text", stop_reason="refusal")
    out = recovery_ladder(LoopState([]), cut, ctx, QueryParams(), None)
    assert out.reason == TransitionReason.REFUSAL_RETRY
    note = out.extra[0].text()
    assert "not a tool failure" in note and "any form" in note  # 01 R32
    again = recovery_ladder(LoopState([out.extra[0]]), cut, ctx, QueryParams(), None)
    assert again.reason == TerminalReason.MODEL_ERROR


def test_other_api_error_surfaces(ctx, scripted):
    scripted.add(fail(APIError(ErrorKind.BAD_REQUEST, "invalid request", status=400)))
    events, reason = drive([Message.user("q")], ctx, deps=make_deps())
    assert reason == TerminalReason.API_ERROR
    assert messages(events)[-1].is_api_error
