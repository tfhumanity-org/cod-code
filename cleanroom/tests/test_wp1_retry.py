"""WP1 retry engine tests (01 R14-R25, R33, R39) via ScriptedModel ``fail(...)`` turns."""

from __future__ import annotations

import random
import threading

import pytest
from wp1_fakes import drive, make_deps, messages, no_sleep

from harnesslab.api.request import FastModeState
from harnesslab.api.retry import (
    MID_STREAM_POLICY,
    AttemptAbandoned,
    FailureCause,
    FallbackChain,
    PartialStreamKept,
    RetryAction,
    RetryCounters,
    StreamProgress,
    _sleep,
    backoff_ms,
    call_with_retries,
    classify_cause,
    decide_mid_stream,
    error_kind_from_status,
    is_retryable,
    max_retries,
    shrink_for_overflow,
)
from harnesslab.model.base import APIError, ModelRequest
from harnesslab.model.scripted import ScriptedTurn, fail, say
from harnesslab.types import (
    ErrorKind,
    Message,
    RetryStatusEvent,
    SystemNoticeEvent,
    TerminalReason,
    TextBlock,
    TombstoneEvent,
)
from harnesslab.util.abort import AbortedError


def _collect(gen):
    out = []
    try:
        for ev in gen:
            out.append(ev)
    except Exception as e:  # noqa: BLE001
        return out, e
    return out, None


def _err(kind=ErrorKind.SERVER_ERROR, status=500, **kw):
    return APIError(kind, kind.value, status=status, **kw)


# ------------------------------------------------------------------ pure policy


def test_max_retries_knobs():
    assert max_retries({}) == 10
    assert max_retries({"CLAUDE_CODE_MAX_RETRIES": "4"}) == 4
    assert max_retries({"CLAUDE_CODE_MAX_RETRIES": "99"}) == 15  # clamped (01 R17)
    assert max_retries({"CLAUDE_CODE_RETRY_WATCHDOG": "1"}) == 300


@pytest.mark.parametrize("attempt,base", [(1, 500), (2, 1000), (3, 2000), (7, 32_000), (12, 32_000)])
def test_backoff_bounds_with_jitter(attempt, base):
    rng = random.Random(7)
    for _ in range(200):
        d = backoff_ms(attempt, rng=rng)
        assert base <= d <= base * 1.25  # <= 25 % jitter, 32 s cap (01 R18)


def test_backoff_respects_retry_after():
    assert backoff_ms(1, retry_after_s=10, rng=random.Random(0)) == 10_000
    assert backoff_ms(7, retry_after_s=1, rng=random.Random(0)) >= 32_000


def test_retryability_order():
    assert not is_retryable(_err(ErrorKind.CREDIT_LIMIT, 429))  # 1 beats everything
    assert is_retryable(_err(ErrorKind.OVERLOADED, 529, headers={"x-should-retry": "false"}))  # 2 before 4
    assert is_retryable(_err(ErrorKind.CONTEXT_OVERFLOW, 400))  # 3
    assert not is_retryable(_err(ErrorKind.SERVER_ERROR, 500, headers={"x-should-retry": "false"}))  # 4 before 8
    assert is_retryable(_err(ErrorKind.BAD_REQUEST, 400, headers={"x-should-retry": "true"}))
    assert is_retryable(_err(ErrorKind.CONNECTION, None))  # 5
    for s in (408, 409, 401):  # 6
        assert is_retryable(_err(ErrorKind.UNKNOWN, s))
    assert is_retryable(_err(ErrorKind.RATE_LIMIT, 429))  # 7 non-subscriber
    assert not is_retryable(_err(ErrorKind.RATE_LIMIT, 429, is_unified_quota=True), is_subscriber=True)
    assert is_retryable(_err(ErrorKind.RATE_LIMIT, 429), is_subscriber=True)
    assert is_retryable(_err(ErrorKind.SERVER_ERROR, 503))  # 8
    for s in (400, 403, 404, 413):
        assert not is_retryable(_err(ErrorKind.BAD_REQUEST, s))
    assert not is_retryable(_err(ErrorKind.PROMPT_TOO_LONG, 400))


def test_shrink_for_overflow():
    assert shrink_for_overflow(150_000, 200_000, 64_000) == 49_000
    with pytest.raises(ValueError):
        shrink_for_overflow(196_500, 200_000, 32_000)  # below the 3000 floor
    with pytest.raises(ValueError):
        shrink_for_overflow(100_000, 200_000, 32_000)  # not smaller than before


def test_mid_stream_table_complete_and_keeps_partial_after_real_output():
    assert len(MID_STREAM_POLICY) == len(StreamProgress) * len(FailureCause)
    for cause in FailureCause:
        a = MID_STREAM_POLICY[(StreamProgress.REAL_OUTPUT, cause)]
        if cause in (FailureCause.BAD_REQUEST, FailureCause.DENIED):
            assert a == RetryAction.FAIL
        else:
            assert a == RetryAction.KEEP_PARTIAL  # 01 R15: no re-billing
    assert MID_STREAM_POLICY[(StreamProgress.STARTED, FailureCause.TIMED_OUT)] == RetryAction.RETRY_NON_STREAMING
    assert MID_STREAM_POLICY[(StreamProgress.STARTED, FailureCause.SERVER_ERROR)] == RetryAction.RETRY


def test_mid_stream_caps():
    c = RetryCounters()
    assert decide_mid_stream(StreamProgress.STARTED, FailureCause.STALLED, c) == RetryAction.RETRY
    assert decide_mid_stream(StreamProgress.STARTED, FailureCause.STALLED, c) == RetryAction.FAIL  # stalls <= 1
    c = RetryCounters()
    assert decide_mid_stream(StreamProgress.STARTED, FailureCause.TRUNCATED, c) == RetryAction.RETRY
    assert decide_mid_stream(StreamProgress.STARTED, FailureCause.TRUNCATED, c) == RetryAction.FAIL
    c = RetryCounters()
    for _ in range(2):
        assert decide_mid_stream(StreamProgress.THINKING_ONLY, FailureCause.SERVER_ERROR, c) == RetryAction.RETRY
    assert decide_mid_stream(StreamProgress.THINKING_ONLY, FailureCause.SERVER_ERROR, c) == RetryAction.KEEP_PARTIAL


def test_classify_and_status_mapping():
    assert classify_cause(_err(ErrorKind.UNKNOWN, 529)) == FailureCause.OVERLOADED
    assert classify_cause(_err(ErrorKind.TIMEOUT, None)) == FailureCause.TIMED_OUT
    assert classify_cause(_err(ErrorKind.NOT_FOUND, 404)) == FailureCause.BAD_REQUEST
    assert error_kind_from_status(400, "prompt is too long: 210000 tokens > 200000 maximum") == ErrorKind.PROMPT_TOO_LONG
    assert error_kind_from_status(529) == ErrorKind.OVERLOADED
    assert error_kind_from_status(429, "credit balance too low") == ErrorKind.CREDIT_LIMIT
    assert error_kind_from_status(None) == ErrorKind.CONNECTION
    assert error_kind_from_status(400, "unsupported beta header") == ErrorKind.BETA_REJECTED


def test_fallback_chain():
    ch = FallbackChain(["a", "a", "b"])
    assert ch.advance() == "b" and ch.advance() is None and ch.current() == "b"


# ------------------------------------------------------------------ through call_with_retries


def test_retries_then_succeeds_with_status_events(ctx, scripted):
    slept = no_sleep(ctx)
    seen = []
    ctx.extra["on_retry"] = seen.append
    scripted.add(fail(_err()), fail(_err()), say("ok"))
    events, err = _collect(call_with_retries(ModelRequest(model="m"), ctx))
    assert err is None
    status = [e for e in events if isinstance(e, RetryStatusEvent)]
    assert [s.attempt for s in status] == [1, 2] and status[0].max_attempts == 10
    assert len(slept) == 2 and 0.5 <= slept[0] <= 0.625 and 1.0 <= slept[1] <= 1.25
    assert seen == status  # callback half of 01 R24


def test_gives_up_after_max_retries(ctx, scripted):
    no_sleep(ctx)
    ctx.env = {"CLAUDE_CODE_MAX_RETRIES": "3"}
    scripted.add(*[fail(_err()) for _ in range(5)])
    events, err = _collect(call_with_retries(ModelRequest(model="m"), ctx))
    assert isinstance(err, APIError) and len(scripted.requests) == 3


def test_retry_after_over_60s_aborts(ctx, scripted):
    no_sleep(ctx)
    scripted.add(fail(_err(ErrorKind.RATE_LIMIT, 429, headers={"retry-after": "120"})))
    _, err = _collect(call_with_retries(ModelRequest(model="m"), ctx))
    assert isinstance(err, APIError) and len(scripted.requests) == 1


def test_overflow_self_heal(ctx, scripted):
    no_sleep(ctx)
    scripted.add(fail(_err(ErrorKind.CONTEXT_OVERFLOW, 400, actual_tokens=180_000, limit_tokens=200_000)), say("ok"))
    _, err = _collect(call_with_retries(ModelRequest(model="m", max_tokens=32_000), ctx))
    assert err is None and scripted.requests[1].max_tokens == 19_000  # 01 R20


def test_overflow_unhealable_fails(ctx, scripted):
    scripted.add(fail(_err(ErrorKind.CONTEXT_OVERFLOW, 400, actual_tokens=197_000, limit_tokens=200_000)))
    _, err = _collect(call_with_retries(ModelRequest(model="m", max_tokens=32_000), ctx))
    assert isinstance(err, APIError)


def test_529_background_fails_immediately(ctx, scripted):
    scripted.add(fail(_err(ErrorKind.OVERLOADED, 529)), say("never"))
    _, err = _collect(call_with_retries(ModelRequest(model="m", query_source="title"), ctx))
    assert isinstance(err, APIError) and len(scripted.requests) == 1  # 01 R21


def test_529_foreground_falls_back_after_three(ctx, scripted):
    no_sleep(ctx)
    ctx.fallback_models = ["backup"]
    scripted.add(*[fail(_err(ErrorKind.OVERLOADED, 529)) for _ in range(3)], say("ok"))
    events, err = _collect(call_with_retries(ModelRequest(model="primary"), ctx))
    assert err is None
    notice = [e for e in events if isinstance(e, SystemNoticeEvent)]
    assert notice[0].data == {"from": "primary", "to": "backup", "reason": "overloaded"}  # 01 R33
    assert [r.model for r in scripted.requests] == ["primary"] * 3 + ["backup"]


def test_529_without_fallback_fails_with_repeated_overload(ctx, scripted):
    no_sleep(ctx)
    scripted.add(*[fail(_err(ErrorKind.OVERLOADED, 529)) for _ in range(3)])
    _, err = _collect(call_with_retries(ModelRequest(model="primary"), ctx))
    assert isinstance(err, APIError) and "repeatedly overloaded" in err.message


def test_no_model_fallback_env(ctx, scripted):
    no_sleep(ctx)
    ctx.env = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"}
    ctx.fallback_models = ["backup"]
    scripted.add(*[fail(_err(ErrorKind.OVERLOADED, 529)) for _ in range(3)])
    _, err = _collect(call_with_retries(ModelRequest(model="primary"), ctx))
    assert isinstance(err, APIError)


def test_last_resort_fallback_on_non_retryable(ctx, scripted):
    ctx.fallback_models = ["backup"]
    scripted.add(fail(_err(ErrorKind.NOT_FOUND, 404)), say("ok"))
    events, err = _collect(call_with_retries(ModelRequest(model="primary", stream=False), ctx))
    assert err is None
    assert [e for e in events if isinstance(e, SystemNoticeEvent)][0].data["reason"] == "model_not_found"


def test_404_streaming_falls_back_to_non_streaming(ctx, scripted):
    scripted.add(fail(_err(ErrorKind.NOT_FOUND, 404)), say("ok"))
    req = ModelRequest(model="m", max_tokens=100_000, thinking={"type": "enabled", "budget_tokens": 90_000})
    _, err = _collect(call_with_retries(req, ctx))
    sent = scripted.requests[1]
    assert err is None and sent.stream is False and sent.max_tokens == 64_000  # 01 R14
    assert sent.thinking["budget_tokens"] == 63_999 and sent.metadata["timeout_s"] == 300


def test_mid_stream_retry_emits_abandon_marker(ctx, scripted):
    no_sleep(ctx)
    scripted.add(ScriptedTurn([TextBlock("")], error=_err(), error_after_events=2), say("ok"))
    events, err = _collect(call_with_retries(ModelRequest(model="m"), ctx))
    assert err is None and any(isinstance(e, AttemptAbandoned) for e in events)


def test_mid_stream_keeps_partial_after_real_output(ctx, scripted):
    scripted.add(ScriptedTurn([TextBlock("real output here")], error=_err(), error_after_events=3), say("never"))
    _, err = _collect(call_with_retries(ModelRequest(model="m"), ctx))
    assert isinstance(err, PartialStreamKept) and len(scripted.requests) == 1


def test_loop_tombstones_abandoned_attempt_and_keeps_partial(ctx, scripted):
    no_sleep(ctx)
    scripted.add(ScriptedTurn([TextBlock("")], error=_err(), error_after_events=2), say("final"))
    events, reason = drive([Message.user("q")], ctx)
    assert reason == TerminalReason.COMPLETED
    assert any(isinstance(e, TombstoneEvent) for e in events)  # 01 R16
    assert [m.text() for m in messages(events)] == ["final"]
    scripted.add(ScriptedTurn([TextBlock("partial answer kept")], error=_err(), error_after_events=4))
    events, reason = drive([Message.user("q")], ctx)
    kept = messages(events)[0]
    assert kept.text().startswith("partial") and kept.api_error.kind == ErrorKind.SERVER_ERROR


def test_sleep_interruptible(ctx):
    wake = threading.Event()
    ctx.extra["retry_wake"] = wake
    threading.Timer(0.05, wake.set).start()
    _sleep(ctx, 5.0)  # returns early on the wake signal (01 R24)
    threading.Timer(0.05, lambda: ctx.abort.abort("user")).start()
    with pytest.raises(AbortedError):
        _sleep(ctx, 5.0)


def test_beta_latching(ctx, scripted):
    scripted.add(fail(APIError(ErrorKind.BETA_REJECTED, "bad beta", status=400, rejected_beta="shiny-2025-01-01")), say("ok"), say("ok"))
    _, err = _collect(call_with_retries(ModelRequest(model="m", betas=["shiny-2025-01-01", "keep"]), ctx))
    assert err is None and scripted.requests[1].betas == ["keep"]
    _collect(call_with_retries(ModelRequest(model="m", betas=["shiny-2025-01-01", "keep"]), ctx))
    assert scripted.requests[2].betas == ["keep"]  # stays off for the conversation (01 R25)


def test_invalid_signature_strips_thinking(ctx, scripted):
    scripted.add(fail(APIError(ErrorKind.BAD_REQUEST, "Invalid signature in thinking block", status=400)), say("ok"))
    msgs = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}, {"type": "text", "text": "a"}]}]
    _, err = _collect(call_with_retries(ModelRequest(model="m", messages=msgs), ctx))
    assert err is None and scripted.requests[1].messages[0]["content"] == [{"type": "text", "text": "a"}]


def test_fast_mode_rate_limit_short_wait_and_cooldown(ctx, scripted):
    slept = no_sleep(ctx)
    scripted.add(fail(_err(ErrorKind.RATE_LIMIT, 429, headers={"retry-after": "5"})), say("ok"))
    _collect(call_with_retries(ModelRequest(model="m", speed="fast"), ctx))
    assert scripted.requests[1].speed == "fast" and slept == [5.0]  # < 20 s: wait, stay fast
    scripted.add(fail(_err(ErrorKind.RATE_LIMIT, 429, headers={"retry-after": "30"})), say("ok"))
    _collect(call_with_retries(ModelRequest(model="m", speed="fast"), ctx))
    assert scripted.requests[3].speed is None
    st: FastModeState = ctx.extra["fast_mode_state"]
    assert st.cooldown_until > 0
    s2 = FastModeState()
    assert s2.on_rate_limited(None, now=0) == ("cooldown", 1800)
    assert s2.on_rate_limited(60, now=0) == ("cooldown", 600)  # min 10 min (01 R39)


def test_fast_mode_unavailable_disables_for_model(ctx, scripted):
    scripted.add(fail(APIError(ErrorKind.BAD_REQUEST, "fast mode is not available", status=400)), say("ok"))
    _collect(call_with_retries(ModelRequest(model="m", speed="fast"), ctx))
    assert scripted.requests[1].speed is None and "m" in ctx.extra["fast_mode_state"].disabled_models
