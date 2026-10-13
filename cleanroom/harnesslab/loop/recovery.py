"""Post-stream recovery ladder (WP1).

Implements spec 01 R26-R32 and 01 §4 ``recovery_ladder``; takeaways 01#T2
(withhold the error row while recovering), 01#T3 (terse continuation nudges,
meta, capped at 3).

Order of checks (first match wins):

1. prompt too long / image too large -> reactive compaction once
   (``REACTIVE_COMPACT_RETRY``) else ``Terminal(PROMPT_TOO_LONG | IMAGE_ERROR)``
   (01 R26); the rapid-refill breaker (01 R27) yields ``RAPID_REFILL_BREAKER``.
2. ``stop_reason == "max_tokens"`` and < 3 recoveries -> nudge
   (``MAX_OUTPUT_TOKENS_RECOVERY``), nudge variant chosen by where the cut fell
   (01 R28).
3. stream truncated in non-interactive / subagent context -> nudge
   (``TRUNCATED_RESPONSE_RECOVERY``, 01 R29).
4. ``stop_reason == "tool_use"`` with no parseable tool block -> tombstone +
   retry once (``MALFORMED_TOOL_USE_RETRY``) else
   ``MALFORMED_TOOL_USE_EXHAUSTED`` (01 R30).
5. thinking-only end -> nudge once (``THINKING_ONLY_RETRY``, 01 R31).
6. output filtered by safety classifier -> explanatory meta note (01 R32).
7. any other API-error row -> ``Terminal(API_ERROR)`` surfacing the row.

Design choices (spec silent):

* After 3 output-limit nudges the turn ends ``COMPLETED`` and a user-visible
  error row naming the limit and ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is surfaced.
* A truncated stream outside non-interactive/subagent contexts surfaces as an
  API error (the interactive user can simply say "continue").
* The classifier-stop note (step 6) continues with ``REFUSAL_RETRY`` once; if the
  very last item already is that note, the turn ends ``MODEL_ERROR``.
* Model-side failures (``NOT_FOUND``, ``REFUSAL``, ``MALFORMED``) end with
  ``MODEL_ERROR``; everything else with ``API_ERROR``.
* Thinking resumption (01 R28 MAY) is not implemented.
"""

from __future__ import annotations

from harnesslab.interfaces import Compactor
from harnesslab.loop import nudges
from harnesslab.loop.state import MAX_OUTPUT_LIMIT_RECOVERIES, Continue, LoopState, QueryParams, Terminal
from harnesslab.runtime import SessionContext
from harnesslab.types import (
    ApiErrorDetail,
    ErrorKind,
    Message,
    TerminalReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    TransitionReason,
)

CLASSIFIER_NOTE_ORIGIN = "classifier-stop"
_MODEL_SIDE = {ErrorKind.NOT_FOUND, ErrorKind.REFUSAL, ErrorKind.MALFORMED}


def _kind(msg: Message | None) -> ErrorKind | None:
    return msg.api_error.kind if msg is not None and msg.api_error is not None else None


def is_prompt_too_long(msg: Message | None) -> bool:
    """The (withheld) error row says the input does not fit (01 R26); oversized
    images take the same path."""
    return bool(msg is not None and msg.is_api_error and _kind(msg) in (ErrorKind.PROMPT_TOO_LONG, ErrorKind.IMAGE_TOO_LARGE))


def parseable_tool_block(msg: Message) -> bool:
    """True if at least one ToolUseBlock parsed (``parse_error is None``)."""
    return any(b.parse_error is None for b in msg.tool_uses())


def _has_visible(msg: Message) -> bool:
    return any((isinstance(b, TextBlock) and b.text.strip()) or isinstance(b, ToolUseBlock) for b in msg.content)


def stream_truncated(msg: Message | None) -> bool:
    """API-error row of kind TRUNCATED after real output began (01 R29).

    Such a row is a *kept partial* (``api.retry.PartialStreamKept``): real
    content plus ``api_error`` detail, not flagged ``is_api_error``.
    """
    return bool(
        msg is not None
        and _kind(msg) in (ErrorKind.TRUNCATED, ErrorKind.CONNECTION, ErrorKind.STALLED, ErrorKind.TIMEOUT)
        and _has_visible(msg)
    )


def cut_kind(msg: Message) -> str:
    """Where a max_tokens cut fell: ``"reasoning"`` (before any text/tool), ``"text"``
    (mid visible text: restart the interrupted line / reopen fences), or
    ``"generic"`` (01 R28)."""
    if not _has_visible(msg):
        return "reasoning" if any(isinstance(b, ThinkingBlock) for b in msg.content) else "generic"
    last = msg.content[-1] if msg.content else None
    if isinstance(last, TextBlock) and last.text.strip():
        return "text"
    return "generic"


def _error_row(text: str, kind: ErrorKind) -> Message:
    return Message.assistant(text, is_api_error=True, api_error=ApiErrorDetail(kind, text))


def recovery_ladder(
    state: LoopState,
    last: Message | None,
    ctx: SessionContext,
    params: QueryParams,
    compactor: Compactor | None,
) -> Continue | Terminal | None:
    """Return ``None`` when the round ended normally (no recovery needed). May mutate
    ``state.guards`` (budgets are per round, 01 R3)."""
    if last is None:
        return None
    g = state.guards
    kind = _kind(last)

    # 1. prompt too long / image too large (01 R26, R27)
    if is_prompt_too_long(last):
        reason = TerminalReason.IMAGE_ERROR if kind == ErrorKind.IMAGE_TOO_LARGE else TerminalReason.PROMPT_TOO_LONG
        if compactor is not None and compactor.thrashing(ctx):
            return Terminal(TerminalReason.RAPID_REFILL_BREAKER, surface=Message.system(nudges.thrash_text()))
        if compactor is not None and not g.tried_reactive_compact:
            g.tried_reactive_compact = True
            g.reactive_compact_depth += 1
            gap = None
            if last.api_error and last.api_error.actual_tokens and last.api_error.limit_tokens:
                gap = last.api_error.actual_tokens - last.api_error.limit_tokens
            result = compactor.reactive(state.items, ctx, gap)
            if result is not None:
                why = (
                    TransitionReason.PRECOMPUTED_COMPACT_SWAP
                    if result.trigger == "precomputed"
                    else TransitionReason.REACTIVE_COMPACT_RETRY
                )
                return Continue(why, replace_items=list(result.items), keep_assistant=False)
        return Terminal(reason, surface=last)

    # 2. output token limit (01 R28)
    if last.stop_reason == "max_tokens" and not last.is_api_error:
        if g.output_limit_recoveries < MAX_OUTPUT_LIMIT_RECOVERIES:
            g.output_limit_recoveries += 1
            return Continue(
                TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY,
                extra=[nudges.meta(nudges.output_limit_nudge(cut_kind(last)))],
                keep_assistant=_has_visible(last),
            )
        limit = int(ctx.extra.get("last_max_tokens") or 0)
        return Terminal(TerminalReason.COMPLETED, surface=_error_row(nudges.output_limit_error_text(limit), ErrorKind.BAD_REQUEST))

    # 3. truncated stream (01 R29)
    if stream_truncated(last):
        if (params.is_subagent or not params.is_interactive) and not g.truncation_retried:
            g.truncation_retried = True
            return Continue(
                TransitionReason.TRUNCATED_RESPONSE_RECOVERY,
                extra=[nudges.meta(nudges.truncation_nudge(params.is_subagent))],
            )
        return Terminal(TerminalReason.API_ERROR, surface=_error_row(last.api_error.message, kind))  # type: ignore[union-attr,arg-type]

    # 4. malformed tool use (01 R30)
    if last.stop_reason == "tool_use" and not last.is_api_error and not parseable_tool_block(last):
        if g.malformed_tool_retried:
            return Terminal(
                TerminalReason.MALFORMED_TOOL_USE_EXHAUSTED,
                surface=_error_row("The model produced an unparseable tool call twice.", ErrorKind.MALFORMED),
            )
        g.malformed_tool_retried = True
        return Continue(
            TransitionReason.MALFORMED_TOOL_USE_RETRY,
            extra=[nudges.meta(nudges.malformed_tool_nudge())],
            tombstone=last,
            keep_assistant=False,
        )

    # 5. thinking-only end (01 R31)
    if not last.is_api_error and last.is_thinking_only() and not g.thinking_only_nudged:
        g.thinking_only_nudged = True
        return Continue(
            TransitionReason.THINKING_ONLY_RETRY,
            extra=[nudges.meta(nudges.visible_output_nudge())],
            tombstone=last,
            keep_assistant=False,
        )

    # 6. safety classifier stop (01 R32)
    if kind in (ErrorKind.OUTPUT_FILTERED, ErrorKind.REFUSAL) or last.stop_reason == "refusal":
        prev = state.items[-1] if state.items else None
        if isinstance(prev, Message) and prev.origin == CLASSIFIER_NOTE_ORIGIN:
            return Terminal(TerminalReason.MODEL_ERROR, surface=last if last.is_api_error else None)
        return Continue(
            TransitionReason.REFUSAL_RETRY,
            extra=[nudges.meta(nudges.classifier_stop_note(), origin=CLASSIFIER_NOTE_ORIGIN)],
            keep_assistant=not last.is_api_error and _has_visible(last),
        )

    # 7. any other API error (01 #T2: surface only now)
    if last.is_api_error:
        reason = TerminalReason.MODEL_ERROR if kind in _MODEL_SIDE else TerminalReason.API_ERROR
        return Terminal(reason, surface=last)
    return None
