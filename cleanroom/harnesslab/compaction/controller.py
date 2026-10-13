"""Per-turn auto-compaction controller with circuit breakers (WP4).

Implements spec 05 R10-R15 and §4.2 (failure breaker after 3 consecutive
failures; prefix-overflow detection "compaction cannot help"; thrash breaker:
refilled to the threshold within < 3 turns of a compaction, 3 times in a row,
-> stop and show an actionable message; larger-window model when blocked;
classifier-overflow forced compaction + "re-issue dropped tool calls"
reminder; precomputed summary swap when still valid, R29), 01 R26/R27
(reactive path + rapid-refill breaker reported to the loop); takeaways 05#T1,
05#T6; P21.

``AutoCompactor`` implements ``interfaces.Compactor``.

The relief ladder (05 §2.1, 05#T1) as seen from ``maybe_autocompact``:

* L1 server edits - request-side, see ``microcompact.server_clearing_edit``;
* L2 micro-compaction - tried first; if it brings the context below the
  threshold, no summary is needed;
* L3 precomputed summary - swapped in when valid;
* L4 threshold compaction - ``summarizer.compact``;
* L5 reactive compaction - ``reactive()`` after a prompt-too-long error.

Inputs a host may put in ``ctx.extra`` (all optional):
``prefix_tokens`` (system prompt + tool schemas estimate, R12),
``threshold_enforced`` (R8, default True), ``classifier_context_overflowed``
(R15, consumed), ``larger_window_model`` = ``(model_id, window)`` (R14).

Design choice: precompute (R29) runs on a daemon thread; ``precompute()`` is
public and synchronous so tests and hosts can drive it deterministically. A
precomputed result is valid while the last row it summarized is still present
in the conversation (the head it covered did not change).

Deferred: idle compaction before cache-TTL expiry (05 R30, needs a host timer);
R14 is reduced to "compact even when blocked if ``larger_window_model`` is set" -
choosing the model for the summary call is left to ``summarize_fn``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from harnesslab.compaction import summarizer
from harnesslab.compaction.microcompact import log_replacements, microcompact
from harnesslab.compaction.summarizer import CompactBlocked, CompactError, SummarizeFn
from harnesslab.compaction.thresholds import (
    Levels,
    auto_compaction_disabled,
    compaction_disabled,
    levels_from_env,
    resolve_window,
)
from harnesslab.interfaces import CompactResult
from harnesslab.runtime import SessionContext
from harnesslab.tokens import context_tokens
from harnesslab.types import Attachment, ConversationItem

FAILURE_BREAKER = 3
THRASH_TURNS = 3
THRASH_STRIKES = 3

THRASH_MESSAGE = (
    "Automatic compaction keeps refilling the context within a few turns, so it has been paused. "
    "A file or tool output is probably too large: read smaller pieces (offset/limit, narrower searches) "
    "or start a fresh session."
)
PREFIX_MESSAGE = (
    "The system prompt and tool definitions alone exceed the auto-compact threshold, so compaction "
    "cannot help. Disable some tools or MCP servers, or use a model with a larger context window."
)
REISSUE_REMINDER = (
    "Context was compacted because the safety check for your last tool calls ran out of room. "
    "Those tool calls were dropped and did not run: issue them again if they are still needed."
)


@dataclass
class CompactionState:
    consecutive_failures: int = 0
    thrash_strikes: int = 0
    turns_since_compact: int | None = None
    prefix_too_large: bool = False
    precomputed: CompactResult | None = None
    precomputed_head_uuid: str | None = None
    notices: list[str] = field(default_factory=list)
    last_levels: Levels | None = None
    precompute_running: bool = False


class AutoCompactor:
    def __init__(self, *, summarize_fn: SummarizeFn | None = None, max_output: int = 32_000, context_window: int = 200_000) -> None:
        self.summarize_fn = summarize_fn
        self.max_output = max_output
        self.context_window = context_window
        self._lock = threading.Lock()

    def state(self, ctx: SessionContext) -> CompactionState:
        """``ctx.compaction_state`` (created on first use)."""
        if not isinstance(ctx.compaction_state, CompactionState):
            ctx.compaction_state = CompactionState()
        return ctx.compaction_state

    # ------------------------------------------------------------ levels

    def levels(self, items: list[ConversationItem], ctx: SessionContext) -> Levels:
        win = resolve_window(ctx.env, ctx.settings, model_id=ctx.model_id, model_window=self.context_window)
        return levels_from_env(ctx.env, win.window, self.context_window, self.max_output, context_tokens(items))

    def _notice(self, st: CompactionState, text: str) -> None:
        if text not in st.notices:
            st.notices.append(text)

    # ------------------------------------------------------------ per turn (05 §4.2)

    def maybe_autocompact(self, items: list[ConversationItem], ctx: SessionContext) -> CompactResult | None:
        st = self.state(ctx)
        self.note_turn(ctx, context_tokens(items))
        if auto_compaction_disabled(ctx.env, ctx.settings):
            return None
        lv = self.levels(items, ctx)
        st.last_levels = lv

        larger = ctx.extra.get("larger_window_model")
        if lv.state == "blocked" and larger:  # R14: the bigger model can still read it all
            return self._run_compact(items, ctx, st)
        if st.consecutive_failures >= FAILURE_BREAKER:  # R11
            return None
        if int(ctx.extra.get("prefix_tokens", 0) or 0) > lv.threshold:  # R12
            st.prefix_too_large = True
            self._notice(st, PREFIX_MESSAGE)
            return None
        if st.thrash_strikes >= THRASH_STRIKES:  # R13
            self._notice(st, THRASH_MESSAGE)
            return None
        if ctx.extra.pop("classifier_context_overflowed", False):  # R15
            res = self._run_compact(items, ctx, st)
            if res is not None:
                res.items.append(Attachment("critical_system_reminder", {"content": REISSUE_REMINDER}))
            return res
        if lv.state in ("compact", "blocked") and ctx.extra.get("threshold_enforced", True):
            # L2 first: lossless clearing may be enough on its own.
            cleared, reps = microcompact(items, ctx, log=False)
            if reps and self.levels(cleared, ctx).state in ("ok", "warn"):
                log_replacements(ctx, reps)
                pre = context_tokens(items)
                return CompactResult(cleared, "micro", pre, context_tokens(cleared))
            # Thrash accounting: compacting again shortly after the last one.
            if st.turns_since_compact is not None and st.turns_since_compact < THRASH_TURNS:
                st.thrash_strikes += 1
                if st.thrash_strikes >= THRASH_STRIKES:
                    self._notice(st, THRASH_MESSAGE)
                    return None
            else:
                st.thrash_strikes = 0
            swapped = self._swap_precomputed(items, st)
            if swapped is not None:
                return swapped
            return self._run_compact(items, ctx, st)
        if (
            ctx.settings.get("precomputeCompactionEnabled", False)
            and lv.state in ("ok", "warn")
            and context_tokens(items) >= lv.precompute
            and st.precomputed is None
            and not st.precompute_running
        ):
            st.precompute_running = True
            snapshot = list(items)
            threading.Thread(target=self.precompute, args=(snapshot, ctx), daemon=True).start()
        return None

    def _run_compact(self, items: list[ConversationItem], ctx: SessionContext, st: CompactionState) -> CompactResult | None:
        try:
            res = summarizer.compact(items, ctx, trigger="auto", summarize_fn=self.summarize_fn)
        except CompactBlocked:
            return None  # a hook said no: not a failure of compaction itself
        except CompactError:
            st.consecutive_failures += 1
            return None
        st.consecutive_failures = 0
        st.turns_since_compact = 0
        st.precomputed, st.precomputed_head_uuid = None, None
        return res

    # ------------------------------------------------------------ precompute (R29)

    def precompute(self, items: list[ConversationItem], ctx: SessionContext) -> CompactResult | None:
        st = self.state(ctx)
        try:
            res = summarizer.compact(items, ctx, trigger="auto", summarize_fn=self.summarize_fn)
        except CompactError:
            res = None
        with self._lock:
            st.precompute_running = False
            if res is not None and items:
                st.precomputed, st.precomputed_head_uuid = res, items[-1].uuid
        return res

    def _swap_precomputed(self, items: list[ConversationItem], st: CompactionState) -> CompactResult | None:
        with self._lock:
            pre, head = st.precomputed, st.precomputed_head_uuid
        if pre is None or head is None:
            return None
        idx = next((i for i, it in enumerate(items) if it.uuid == head), None)
        if idx is None:  # conversation head changed (rewind, edit): summary is stale
            st.precomputed, st.precomputed_head_uuid = None, None
            return None
        newer = items[idx + 1 :]
        merged = [*pre.items, *summarizer._fresh_copies(newer)]
        st.precomputed, st.precomputed_head_uuid = None, None
        st.turns_since_compact = 0
        st.consecutive_failures = 0
        return CompactResult(merged, "precomputed", context_tokens(items), context_tokens(merged), pre.boundary)

    # ------------------------------------------------------------ reactive / breakers

    def reactive(self, items: list[ConversationItem], ctx: SessionContext, token_gap: int | None) -> CompactResult | None:
        """01 R26 / 05 R26: after a real prompt-too-long error."""
        if compaction_disabled(ctx.env):
            return None
        st = self.state(ctx)
        res = summarizer.reactive_compact(items, ctx, token_gap=token_gap, summarize_fn=self.summarize_fn)
        if res is not None:
            st.turns_since_compact = 0
        return res

    def thrashing(self, ctx: SessionContext) -> bool:
        """01 R27 / 05 R13: the rapid-refill breaker tripped."""
        return self.state(ctx).thrash_strikes >= THRASH_STRIKES

    def note_turn(self, ctx: SessionContext, tokens: int) -> None:
        """Track refill speed after a compaction (05 R13): one tick per turn."""
        st = self.state(ctx)
        if st.turns_since_compact is not None:
            st.turns_since_compact += 1
