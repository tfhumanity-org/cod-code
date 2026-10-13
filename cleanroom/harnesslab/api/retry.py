"""Retry engine: retryability rules, backoff, mid-stream policy table, fallback (WP1).

Implements spec 01 R14-R25, R33, R39 and 01 §4 ``backoff_ms`` / ``is_retryable``
/ ``shrink_for_overflow``; takeaways 01#T6 (retry as a policy table), 01#T7
(shed background load on overload), 01#T10 (latched betas); P03.

Two layers:

* **Request level** (nothing streamed yet): ``is_retryable`` in the 01 R19 order,
  ``backoff_ms`` (500 ms * 2^(n-1), cap 32 s, +<=25 % jitter, >= retry-after),
  context-overflow self-heal via ``shrink_for_overflow`` (01 R20), 529 handling
  by ``query_source`` (01 R21), fallback after 3 consecutive 529s.
* **Mid-stream** (something streamed): ``MID_STREAM_POLICY[(progress, cause)]``
  -> ``RetryAction`` with per-attempt caps (stalls <= 1, truncations <= 1,
  thinking-only retries <= 2) (01 R15). Abandoned attempts emit tombstones (R16).

``call_with_retries`` is the ``QueryDeps.call_model`` implementation: it yields
raw ``StreamEvent``s from the successful attempt interleaved with
``RetryStatusEvent`` / ``TombstoneEvent`` / ``SystemNoticeEvent`` (model switch)
loop events. On final failure it yields nothing more and raises ``APIError``;
the loop converts that into an API-error row (01 §2.4).

Protocol with the loop (design choices, spec silent):

* Before re-sending after something was streamed, ``call_with_retries`` yields
  an ``AttemptAbandoned`` marker. The retry layer does not know which rows the
  loop emitted, so the *loop* turns the marker into ``TombstoneEvent``s for the
  partial assistant row and discards in-flight tools (01 R16, R33).
* "Keep partial" is signalled by raising ``PartialStreamKept``; the loop then
  finalises what it accumulated as a real assistant row carrying the error
  detail, so the recovery ladder can nudge (01 R29, R32).
* A model switch is a ``SystemNoticeEvent(subtype="model_fallback",
  data={"from", "to", "reason"})``; the loop keeps the new model for the rest of
  the turn (01 R33, R35).
* Sleeps go through ``ctx.extra["retry_sleep"](seconds) -> aborted`` when set
  (tests), else wait on the abort signal and an optional wake event
  ``ctx.extra["retry_wake"]`` (01 R24). ``ctx.extra["on_retry"]`` receives each
  ``RetryStatusEvent`` (the "callback" half of 01 R24).

Deferred: stream liveness timers (01 R13) belong to a real network adapter;
the policy table already handles their outcomes (``STALLED`` / ``TIMEOUT``).
Cloud-provider / API-key-helper / workload-identity refresh (01 R22) share the
single pluggable ``ctx.extra["refresh_credentials"](kind) -> bool`` hook.
Stripping media / advisor blocks / prompt-end markers on targeted rejections
(01 R25 "other cases") is not modelled; proxy-refused system messages cannot
occur because reminders are always sent user-side (``loop.normalize``).
"""

from __future__ import annotations

import enum
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Literal, Mapping

from harnesslab.api.request import fast_mode_state
from harnesslab.config import env_int, env_truthy
from harnesslab.model.base import (
    APIError,
    ContentBlockDelta,
    ContentBlockStart,
    MessageStart,
    ModelRequest,
    StreamEvent,
)
from harnesslab.runtime import SessionContext
from harnesslab.types import ErrorKind, LoopEvent, RetryStatusEvent, SystemNoticeEvent, ToolUseBlock
from harnesslab.util.abort import AbortedError

log = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 10
MAX_RETRIES_CLAMP = 15
PERSISTENT_MAX_RETRIES = 300
BACKOFF_BASE_MS = 500
BACKOFF_CAP_MS = 32_000
RETRY_AFTER_ABORT_S = 60
CONSECUTIVE_529_BEFORE_FALLBACK = 3
OVERFLOW_FLOOR = 3000
OVERFLOW_MARGIN = 1000
NONSTREAMING_MAX_TOKENS = 64_000
NONSTREAMING_TIMEOUT_S = 300
NONSTREAMING_TIMEOUT_REMOTE_S = 120
PERSISTENT_WAIT_CAP_MS = 6 * 60 * 60 * 1000
PERSISTENT_CAPACITY_CAP_MS = 5 * 60 * 1000
PERSISTENT_STATUS_EVERY_S = 30
MAX_CREDENTIAL_REFRESHES = 2

#: 01 R21: sources that keep retrying on 529. Everything else fails at once.
FOREGROUND_SOURCES = frozenset(
    {"main", "sdk", "subagent", "compact", "hook", "side_question", "web", "classifier"}
)

#: Errors the *loop* recovers from (compaction, nudges); never a reason to switch model.
_LOOP_RECOVERABLE = frozenset({ErrorKind.PROMPT_TOO_LONG, ErrorKind.IMAGE_TOO_LARGE, ErrorKind.CONTEXT_OVERFLOW})


class StreamProgress(str, enum.Enum):
    NOTHING = "nothing"
    STARTED = "started"
    PARTIAL = "partial"
    THINKING_ONLY = "thinking_only"
    REAL_OUTPUT = "real_output"


class FailureCause(str, enum.Enum):
    OVERLOADED = "overloaded"
    STALLED = "stalled"
    TRUNCATED = "truncated"
    SUSPENDED = "suspended"
    CONNECTION_LOST = "connection_lost"
    SERVER_ERROR = "server_error"
    TIMED_OUT = "timed_out"
    MALFORMED = "malformed"
    BAD_REQUEST = "bad_request"
    OUTPUT_FILTERED = "output_filtered"
    DENIED = "denied"


class RetryAction(str, enum.Enum):
    RETRY = "retry"
    RETRY_NON_STREAMING = "retry_non_streaming"
    KEEP_PARTIAL = "keep_partial"
    FALLBACK_MODEL = "fallback_model"
    FAIL = "fail"


def _build_policy() -> dict[tuple[StreamProgress, FailureCause], RetryAction]:
    """Fill the table from the 01 R15 principle (architect gap G3):

    * nothing useful streamed yet -> transient causes retry; a timeout or a
      malformed stream retries *without* streaming; request/policy errors fail.
    * real output streamed -> keep the partial output (never pay twice), except
      for errors that say the request itself is wrong or forbidden.
    """
    transient = {
        FailureCause.OVERLOADED, FailureCause.STALLED, FailureCause.TRUNCATED,
        FailureCause.SUSPENDED, FailureCause.CONNECTION_LOST, FailureCause.SERVER_ERROR,
    }
    non_stream = {FailureCause.TIMED_OUT, FailureCause.MALFORMED}
    fatal = {FailureCause.BAD_REQUEST, FailureCause.DENIED}
    table: dict[tuple[StreamProgress, FailureCause], RetryAction] = {}
    for p in StreamProgress:
        for c in FailureCause:
            if p == StreamProgress.REAL_OUTPUT:
                a = RetryAction.FAIL if c in fatal else RetryAction.KEEP_PARTIAL
            elif c in transient:
                a = RetryAction.RETRY
            elif c in non_stream:
                a = RetryAction.RETRY_NON_STREAMING
            else:  # fatal, or output filtered before anything visible
                a = RetryAction.FAIL
            table[(p, c)] = a
    return table


#: (progress, cause) -> action (01 R15). Every pair is present.
MID_STREAM_POLICY: dict[tuple[StreamProgress, FailureCause], RetryAction] = _build_policy()


@dataclass
class RetryCounters:
    attempt: int = 0
    consecutive_529: int = 0
    stalls: int = 0  # cap 1
    truncations: int = 0  # cap 1
    thinking_only_retries: int = 0  # cap 2
    oauth_refreshes: int = 0  # cap 2 (01 R22)
    disable_keepalive: bool = False  # 01 R23
    latched_off_betas: set[str] = field(default_factory=set)  # 01 R25
    stripped_thinking: bool = False
    nonstreaming: bool = False


# ------------------------------------------------------------------ pure policy


def max_retries(env: Mapping[str, str]) -> int:
    """01 R17: default 10; ``CLAUDE_CODE_MAX_RETRIES`` clamped to 15; watchdog mode 300."""
    if env_truthy(env, "CLAUDE_CODE_RETRY_WATCHDOG"):
        return PERSISTENT_MAX_RETRIES
    n = env_int(env, "CLAUDE_CODE_MAX_RETRIES", DEFAULT_MAX_RETRIES, lo=0)
    if n > MAX_RETRIES_CLAMP:
        log.warning("CLAUDE_CODE_MAX_RETRIES=%s is above %s; using %s", n, MAX_RETRIES_CLAMP, MAX_RETRIES_CLAMP)
        n = MAX_RETRIES_CLAMP
    return n


def backoff_ms(
    attempt: int,
    retry_after_s: float | None = None,
    *,
    base: int = BACKOFF_BASE_MS,
    cap: int = BACKOFF_CAP_MS,
    rng: random.Random | None = None,
) -> float:
    """01 R18 / §4: ``min(cap, base*2^(attempt-1))`` + up to 25 % jitter, raised to
    at least ``retry-after``."""
    d = float(min(cap, base * 2 ** max(0, attempt - 1)))
    d += (rng or random).uniform(0, 0.25 * d)
    if retry_after_s is not None:
        d = max(d, retry_after_s * 1000)
    return d


def _is_overloaded(err: APIError) -> bool:
    return err.kind == ErrorKind.OVERLOADED or err.status == 529


def is_retryable(err: APIError, *, is_subscriber: bool = False) -> bool:
    """01 R19, evaluated in order, first match wins."""
    if err.kind == ErrorKind.CREDIT_LIMIT:  # 1 hard credit / spend cap
        return False
    if _is_overloaded(err):  # 2
        return True
    if err.kind == ErrorKind.CONTEXT_OVERFLOW:  # 3 (with adjusted max_tokens)
        return True
    should = (err.headers.get("x-should-retry") or "").lower()  # 4
    if should in ("true", "false"):
        return should == "true"
    if err.kind in (ErrorKind.CONNECTION, ErrorKind.TIMEOUT):  # 5
        return True
    if err.status in (408, 409, 401):  # 6 (401 refreshes credentials first)
        return True
    if err.status == 429 or err.kind == ErrorKind.RATE_LIMIT:  # 7
        return not is_subscriber or not err.is_unified_quota
    if err.kind in (ErrorKind.PROMPT_TOO_LONG, ErrorKind.BILLING, ErrorKind.OUTPUT_FILTERED, ErrorKind.IMAGE_TOO_LARGE):
        return False
    if err.status is not None:  # 8
        return err.status >= 500
    return err.kind == ErrorKind.SERVER_ERROR


def shrink_for_overflow(input_tokens: int, limit: int, prev_max: int) -> int:
    """01 R20: ``limit - input - 1000``; raise ``ValueError`` if < 3000 or not smaller."""
    new = limit - input_tokens - OVERFLOW_MARGIN
    if new < OVERFLOW_FLOOR or new >= prev_max:
        raise ValueError(f"context overflow cannot be healed (max_tokens would be {new})")
    return new


_CAUSE_BY_KIND = {
    ErrorKind.OVERLOADED: FailureCause.OVERLOADED,
    ErrorKind.RATE_LIMIT: FailureCause.OVERLOADED,
    ErrorKind.STALLED: FailureCause.STALLED,
    ErrorKind.TRUNCATED: FailureCause.TRUNCATED,
    ErrorKind.CONNECTION: FailureCause.CONNECTION_LOST,
    ErrorKind.SERVER_ERROR: FailureCause.SERVER_ERROR,
    ErrorKind.UNKNOWN: FailureCause.SERVER_ERROR,
    ErrorKind.TIMEOUT: FailureCause.TIMED_OUT,
    ErrorKind.MALFORMED: FailureCause.MALFORMED,
    ErrorKind.OUTPUT_FILTERED: FailureCause.OUTPUT_FILTERED,
    ErrorKind.REFUSAL: FailureCause.OUTPUT_FILTERED,
    ErrorKind.AUTH: FailureCause.DENIED,
    ErrorKind.PERMISSION: FailureCause.DENIED,
    ErrorKind.CREDIT_LIMIT: FailureCause.DENIED,
    ErrorKind.BILLING: FailureCause.DENIED,
}


def classify_cause(err: APIError) -> FailureCause:
    """Map an error to the mid-stream cause axis of the policy table (01 R15).
    Everything request-shaped (400, 404, overflow, beta rejection) is ``BAD_REQUEST``."""
    if err.status == 529:
        return FailureCause.OVERLOADED
    return _CAUSE_BY_KIND.get(err.kind, FailureCause.BAD_REQUEST)


def decide_mid_stream(progress: StreamProgress, cause: FailureCause, counters: RetryCounters) -> RetryAction:
    """Look up ``MID_STREAM_POLICY`` and apply the per-attempt caps (01 R15).

    A capped retry downgrades to *keep partial* when something worth keeping
    exists (thinking or real output) and to *fail* otherwise.
    """
    action = MID_STREAM_POLICY.get((progress, cause), RetryAction.FAIL)
    if action != RetryAction.RETRY:
        return action
    downgrade = (
        RetryAction.KEEP_PARTIAL
        if progress in (StreamProgress.THINKING_ONLY, StreamProgress.REAL_OUTPUT)
        else RetryAction.FAIL
    )
    if cause == FailureCause.STALLED:
        if counters.stalls >= 1:
            return downgrade
        counters.stalls += 1
    elif cause == FailureCause.TRUNCATED:
        if counters.truncations >= 1:
            return downgrade
        counters.truncations += 1
    if progress == StreamProgress.THINKING_ONLY:
        if counters.thinking_only_retries >= 2:
            return downgrade
        counters.thinking_only_retries += 1
    return action


@dataclass
class FallbackChain:
    """01 R33: primary model then configured fallbacks; disabled by
    ``CLAUDE_CODE_NO_MODEL_FALLBACK``."""

    models: list[str]
    index: int = 0

    def current(self) -> str:
        return self.models[self.index]

    def advance(self) -> str | None:
        """Move to the next *different* model; ``None`` when the chain is exhausted."""
        cur = self.current()
        while self.index + 1 < len(self.models):
            self.index += 1
            if self.models[self.index] != cur:
                return self.models[self.index]
        return None

    @classmethod
    def for_request(cls, model: str, ctx: SessionContext) -> "FallbackChain":
        if env_truthy(ctx.env, "CLAUDE_CODE_NO_MODEL_FALLBACK"):
            return cls([model])
        return cls([model] + [m for m in ctx.fallback_models if m != model])


def error_kind_from_status(status: int | None, message: str = "") -> ErrorKind:
    """Helper for adapters: map an HTTP status (+ message sniffing) to ``ErrorKind``."""
    m = message.lower()
    if "prompt is too long" in m or "prompt too long" in m:
        return ErrorKind.PROMPT_TOO_LONG
    if "max_tokens" in m and ("context limit" in m or "context window" in m):
        return ErrorKind.CONTEXT_OVERFLOW
    if "image" in m and ("too large" in m or "exceeds" in m):
        return ErrorKind.IMAGE_TOO_LARGE
    if "credit balance" in m or "spend limit" in m or "billing" in m:
        return ErrorKind.CREDIT_LIMIT if status == 429 else ErrorKind.BILLING
    if status == 529 or "overloaded" in m:
        return ErrorKind.OVERLOADED
    if status is None:
        return ErrorKind.CONNECTION
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status == 401:
        return ErrorKind.AUTH
    if status == 403:
        return ErrorKind.PERMISSION
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 408:
        return ErrorKind.TIMEOUT
    if status == 400:
        return ErrorKind.BETA_REJECTED if "beta" in m else ErrorKind.BAD_REQUEST
    if status == 413:
        return ErrorKind.PROMPT_TOO_LONG
    if status >= 500:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.UNKNOWN


# ------------------------------------------------------------------ request rewrites


def non_streaming_request(req: ModelRequest, env: Mapping[str, str]) -> ModelRequest:
    """01 R14: non-streaming retry; max_tokens <= 64k, thinking budget <= max_tokens-1,
    timeout from ``API_TIMEOUT_MS`` else 300 s (120 s remote)."""
    max_tokens = min(req.max_tokens, NONSTREAMING_MAX_TOKENS)
    thinking = req.thinking
    if thinking and thinking.get("type") == "enabled":
        thinking = {**thinking, "budget_tokens": min(thinking.get("budget_tokens", 0), max_tokens - 1)}
    default_s = NONSTREAMING_TIMEOUT_REMOTE_S if env_truthy(env, "CLAUDE_CODE_REMOTE") else NONSTREAMING_TIMEOUT_S
    timeout_s = env_int(env, "API_TIMEOUT_MS", default_s * 1000) / 1000
    return req.replace(stream=False, max_tokens=max_tokens, thinking=thinking, metadata={**req.metadata, "timeout_s": timeout_s})


def strip_thinking(req: ModelRequest) -> ModelRequest:
    """01 R25: after an invalid-signature rejection, drop every thinking block."""
    msgs = []
    for m in req.messages:
        content = m.get("content")
        if isinstance(content, list):
            kept = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
            m = {**m, "content": kept or [{"type": "text", "text": "(no content)"}]}
        msgs.append(m)
    return req.replace(messages=msgs)


def _is_signature_error(err: APIError) -> bool:
    m = err.message.lower()
    return err.kind in (ErrorKind.BAD_REQUEST, ErrorKind.UNKNOWN) and "signature" in m and "thinking" in m


def _is_fast_unavailable(err: APIError) -> bool:
    m = err.message.lower()
    return err.status == 400 and ("fast mode" in m or "speed" in m)


# ------------------------------------------------------------------ the retry loop


@dataclass
class AttemptAbandoned:
    """Loop-internal marker: the partial attempt is void (01 R16). Never forwarded."""

    reason: str
    error: APIError | None = None
    kind: ClassVar[str] = "attempt_abandoned"


class PartialStreamKept(Exception):
    """Policy says keep what streamed (01 R15); ``error`` describes the cut."""

    def __init__(self, error: APIError) -> None:
        super().__init__(error.message)
        self.error = error


class _Progress:
    """How far a stream got, *without* touching block objects (the loop's
    ``StreamAccumulator`` owns those)."""

    def __init__(self) -> None:
        self.started = self.real = self.thinking = False

    def feed(self, ev: StreamEvent) -> None:
        if isinstance(ev, (MessageStart, ContentBlockStart)):
            self.started = True
            if isinstance(ev, ContentBlockStart) and isinstance(ev.block, ToolUseBlock):
                self.real = True
        elif isinstance(ev, ContentBlockDelta) and ev.value:
            if ev.delta_type in ("text", "input_json"):
                self.real = True
            elif ev.delta_type == "thinking":
                self.thinking = True

    @property
    def value(self) -> StreamProgress:
        if self.real:
            return StreamProgress.REAL_OUTPUT
        if self.thinking:
            return StreamProgress.THINKING_ONLY
        return StreamProgress.STARTED if self.started else StreamProgress.NOTHING


@dataclass
class RetryPlan:
    """What to do after one failed attempt."""

    kind: Literal["retry", "fail", "keep_partial"]
    request: ModelRequest | None = None
    delay_ms: float = 0.0
    notice: SystemNoticeEvent | None = None
    error: APIError | None = None


def _switch(req: ModelRequest, to: str, reason: str) -> RetryPlan:
    notice = SystemNoticeEvent(
        "model_fallback",
        f"Switched model from {req.model} to {to} ({reason}).",
        {"from": req.model, "to": to, "reason": reason},
    )
    return RetryPlan("retry", req.replace(model=to), 0.0, notice)


_SWITCH_REASON = {
    ErrorKind.NOT_FOUND: "model_not_found",
    ErrorKind.PERMISSION: "permission_denied",
    ErrorKind.REFUSAL: "model_blocked",
}


def plan_retry(
    err: APIError,
    req: ModelRequest,
    progress: StreamProgress,
    counters: RetryCounters,
    chain: FallbackChain,
    ctx: SessionContext,
    *,
    limit: int,
    persistent: bool = False,
) -> RetryPlan:
    """Decide the next step after a failed attempt (01 R14-R25, R33, R39).

    Pure apart from bookkeeping on ``counters`` / ``chain`` / session latches, so
    each branch is unit-testable without streaming.
    """
    env = ctx.env
    overloaded = _is_overloaded(err)
    foreground = req.query_source in FOREGROUND_SOURCES
    fail = RetryPlan("fail", error=err)

    if progress != StreamProgress.NOTHING:
        # ---- mid-stream: policy table (01 R15)
        if overloaded and not foreground:
            return fail
        action = decide_mid_stream(progress, classify_cause(err), counters)
        if overloaded and action == RetryAction.RETRY:
            counters.consecutive_529 += 1
            if counters.consecutive_529 >= CONSECUTIVE_529_BEFORE_FALLBACK:
                action = RetryAction.FALLBACK_MODEL
        if action == RetryAction.KEEP_PARTIAL:
            return RetryPlan("keep_partial", error=err)
        if action == RetryAction.FAIL:
            return fail
        if action == RetryAction.FALLBACK_MODEL:
            nxt = chain.advance()
            if nxt is None:
                return RetryPlan("fail", error=APIError(ErrorKind.OVERLOADED, "The model is repeatedly overloaded.", status=529))
            counters.consecutive_529 = 0
            return _switch(req, nxt, "overloaded")
        if action == RetryAction.RETRY_NON_STREAMING:
            if env_truthy(env, "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK") or not req.stream:
                return fail
            notice = SystemNoticeEvent("nonstreaming_fallback", "The stream ended early; retrying without streaming.")
            return RetryPlan("retry", non_streaming_request(req, env), 0.0, notice)
        # RETRY falls through to the shared attempt cap / backoff below
    else:
        # ---- request level: targeted fixes first (they cost no backoff)
        latched = ctx.extra.setdefault("latched_off_betas", set())
        if err.kind == ErrorKind.BETA_REJECTED and err.rejected_beta and err.rejected_beta not in latched:
            latched.add(err.rejected_beta)  # 01 R25: latched off for the conversation
            counters.latched_off_betas.add(err.rejected_beta)
            return RetryPlan("retry", req.replace(betas=[b for b in req.betas if b != err.rejected_beta]))
        if _is_signature_error(err) and not counters.stripped_thinking:
            counters.stripped_thinking = True
            return RetryPlan("retry", strip_thinking(req))
        if req.speed == "fast" and _is_fast_unavailable(err):
            fast_mode_state(ctx).disable(req.model)  # 01 R39: permanently for this model
            return RetryPlan("retry", req.replace(speed=None))
        if err.kind == ErrorKind.CONTEXT_OVERFLOW:
            if err.actual_tokens is None or err.limit_tokens is None:
                return fail
            try:
                new_max = shrink_for_overflow(err.actual_tokens, err.limit_tokens, req.max_tokens)
            except ValueError:
                return fail
            return RetryPlan("retry", req.replace(max_tokens=new_max))  # 01 R20
        if (err.kind == ErrorKind.NOT_FOUND or err.status == 404) and req.stream and not env_truthy(
            env, "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"
        ):
            notice = SystemNoticeEvent("nonstreaming_fallback", "Streaming is unavailable; retrying without streaming.")
            return RetryPlan("retry", non_streaming_request(req, env), 0.0, notice)  # 01 R14
        if req.speed == "fast" and (err.status in (429, 529) or err.kind in (ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED)):
            what, secs = fast_mode_state(ctx).on_rate_limited(err.retry_after_s)  # 01 R39
            if what == "wait":
                return RetryPlan("retry", req, secs * 1000)
            notice = SystemNoticeEvent("fast_mode_cooldown", f"Fast mode paused for {int(secs)} s; continuing at normal speed.")
            return RetryPlan("retry", req.replace(speed=None), 0.0, notice)

        if overloaded:  # 01 R21
            if not foreground:
                return fail
            counters.consecutive_529 += 1
            if counters.consecutive_529 >= CONSECUTIVE_529_BEFORE_FALLBACK:
                nxt = chain.advance()
                if nxt is None:
                    return RetryPlan(
                        "fail", error=APIError(ErrorKind.OVERLOADED, "The model is repeatedly overloaded.", status=529)
                    )
                counters.consecutive_529 = 0
                return _switch(req, nxt, "overloaded")
        else:
            counters.consecutive_529 = 0

        if not is_retryable(err, is_subscriber=bool(ctx.extra.get("is_subscriber"))):
            if err.kind not in _LOOP_RECOVERABLE:  # 01 R19 last-resort fallback
                nxt = chain.advance()
                if nxt is not None:
                    return _switch(req, nxt, _SWITCH_REASON.get(err.kind, "last_resort"))
            return fail

        if err.status == 401 or err.kind == ErrorKind.AUTH:  # 01 R22
            if counters.oauth_refreshes >= MAX_CREDENTIAL_REFRESHES:
                return fail
            counters.oauth_refreshes += 1
            refresh = ctx.extra.get("refresh_credentials")
            if callable(refresh):
                refresh("oauth")
        if err.kind == ErrorKind.CONNECTION and any(s in err.message.lower() for s in ("reset", "stale", "socket hang up")):
            counters.disable_keepalive = True  # 01 R23

    # ---- shared: attempt cap and backoff (01 R17, R18)
    if counters.attempt >= limit:
        return fail
    ra = err.retry_after_s
    if ra is not None and ra > RETRY_AFTER_ABORT_S and not persistent:
        return fail
    base = env_int(env, "CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS", BACKOFF_BASE_MS) if overloaded else BACKOFF_BASE_MS
    cap = BACKOFF_CAP_MS
    if persistent and (overloaded or err.status == 429):
        cap = PERSISTENT_CAPACITY_CAP_MS
    delay = backoff_ms(counters.attempt, ra, base=base, cap=cap, rng=ctx.extra.get("retry_rng"))
    if persistent:
        delay = min(delay, PERSISTENT_WAIT_CAP_MS)
    return RetryPlan("retry", req, delay)


def _sleep(ctx: SessionContext, seconds: float) -> None:
    """Interruptible sleep (01 R24): abort raises, a wake event cuts it short."""
    fn = ctx.extra.get("retry_sleep")
    if callable(fn):
        if fn(seconds) or ctx.abort.signal.aborted:
            raise AbortedError(ctx.abort.signal.reason or "aborted")
        return
    wake = ctx.extra.get("retry_wake")
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if ctx.abort.signal.wait(min(left, 0.05)):
            raise AbortedError(ctx.abort.signal.reason or "aborted")
        if wake is not None and wake.is_set():
            wake.clear()
            return


def _emit_status(ctx: SessionContext, ev: RetryStatusEvent) -> RetryStatusEvent:
    cb = ctx.extra.get("on_retry")
    if callable(cb):
        cb(ev)
    return ev


def call_with_retries(request: ModelRequest, ctx: SessionContext) -> Iterator[StreamEvent | LoopEvent | Any]:
    """The retrying model call (``QueryDeps.call_model``). Sleeps are interruptible
    via ``ctx.abort.signal.wait`` (01 R24). Uses ``ctx.model``."""
    if ctx.model is None:
        raise RuntimeError("SessionContext.model is not set")
    env = ctx.env
    persistent = env_truthy(env, "CLAUDE_CODE_RETRY_WATCHDOG")
    limit = max_retries(env)
    counters = RetryCounters()
    chain = FallbackChain.for_request(request.model, ctx)
    req = request
    while True:
        counters.attempt += 1
        latched = ctx.extra.get("latched_off_betas") or set()
        req = req.replace(betas=[b for b in req.betas if b not in latched])
        if counters.disable_keepalive:
            req = req.replace(metadata={**req.metadata, "disable_keepalive": True})
        progress = _Progress()
        try:
            for ev in ctx.model.stream(req, ctx.abort.signal):
                progress.feed(ev)
                yield ev
            return
        except APIError as err:
            if ctx.abort.signal.aborted:
                raise AbortedError(ctx.abort.signal.reason or "aborted") from err
            plan = plan_retry(err, req, progress.value, counters, chain, ctx, limit=limit, persistent=persistent)
            if plan.kind == "fail":
                raise (plan.error or err) from (None if plan.error is err else err)
            if plan.kind == "keep_partial":
                raise PartialStreamKept(err) from err
            if progress.started:
                yield AttemptAbandoned("retry", err)  # loop tombstones partial rows (01 R16)
            if plan.notice is not None:
                yield plan.notice
            if plan.delay_ms > 0:
                remaining = plan.delay_ms / 1000
                while True:  # persistent mode re-emits the status every 30 s (01 R18)
                    yield _emit_status(ctx, RetryStatusEvent(err.message, remaining * 1000, counters.attempt, limit))
                    chunk = min(remaining, PERSISTENT_STATUS_EVERY_S) if persistent else remaining
                    _sleep(ctx, chunk)
                    remaining -= chunk
                    if remaining <= 0:
                        break
            assert plan.request is not None
            req = plan.request
