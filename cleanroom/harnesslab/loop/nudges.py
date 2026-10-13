"""Synthetic texts the loop injects: nudges, interruption notes, placeholders (WP1).

Spec gives *required content*, never wording (INDEX: "implementers write their
own text"). Each function returns this implementation's own prose and every
message built from it is ``is_meta=True`` (01 §2.5).

Implements: 01 R28 (three output-limit variants), R29 (truncation, subagent
rewrite variant), R30 (re-emit tool call), R31 (ask for visible output), R32
(safety-classifier stop), R51 (interruption, two variants), R54 (mid-turn user
input), R6 (stop-hook cap warning mentioning ``stop_hook_active``). Takeaway
01#T3. Synthetic tool-result texts (01 R52) are FINAL in
``types.SYNTHETIC_RESULT_TEXT``.

Style rule for every nudge (01#T3): short, imperative, no apology requested,
no recap requested. The model reads these, so each one says exactly what
happened and exactly what to do next.
"""

from __future__ import annotations

from harnesslab.types import Message


def output_limit_nudge(kind: str) -> str:
    """``kind`` from ``recovery.cut_kind``: generic | reasoning | text (01 R28).

    Required content per variant:

    * generic   - resume at once, no apology, no recap; continue mid-thought;
                  split the remaining work into smaller pieces.
    * reasoning - the reasoning was discarded; think much more briefly.
    * text      - restart at the beginning of the interrupted line / row / list
                  item / sentence and reopen any open code fence or table.
    """
    if kind == "reasoning":
        return (
            "Your output limit was reached while you were still reasoning, before any "
            "visible answer or tool call, so that reasoning was discarded. Reason far "
            "more briefly this time and get to the answer or the next tool call quickly."
        )
    if kind == "text":
        return (
            "Your reply hit the output limit mid-text. Continue right away with no apology "
            "and no summary of what came before. Whatever unit was interrupted - a sentence, a "
            "bullet, a table line or a code line - rewrite it from its start, and if a code fence or "
            "table was open, open it again first. Break the remaining work into smaller pieces."
        )
    return (
        "Your reply hit the output token limit. Continue directly from where it stopped, "
        "even if that is mid-thought. Do not apologise and do not recap. Split whatever "
        "work remains into smaller steps so each reply fits."
    )


def truncation_nudge(is_subagent: bool) -> str:
    """01 R29: the stream was cut after output began."""
    if is_subagent:
        return (
            "The connection dropped while your previous reply was streaming, so it is "
            "incomplete. Write your full reply again from the start, complete this time."
        )
    return (
        "The connection dropped while your previous reply was streaming. Resume at the "
        "exact point where the text stops. If nothing useful survived, answer from scratch."
    )


def malformed_tool_nudge() -> str:
    """01 R30: the tool call did not parse; emit it again."""
    return (
        "Your last tool call could not be parsed, so it was not run. Emit the tool call "
        "again with valid JSON input that matches the tool's schema."
    )


def visible_output_nudge() -> str:
    """01 R31: the turn ended with thinking only."""
    return (
        "Your last reply contained only internal reasoning and nothing the user can see. "
        "Now write a visible response (or make the next tool call)."
    )


def classifier_stop_note() -> str:
    """01 R32: a safety classifier stopped the output. Not a tool or API error."""
    return (
        "Your previous output was stopped by a safety classifier. This was a policy "
        "decision, not a tool failure or an API error, so do not retry it. Do not produce "
        "that content again in any form or wording."
    )


def interruption_text(during_tools: bool) -> str:
    """01 R51: shown as a user row after an abort; two variants."""
    if during_tools:
        return "[The user interrupted while tools were running. Pending tool calls were stopped.]"
    return "[The user interrupted the response.]"


def midturn_input_text(texts: list[str]) -> str:
    """01 R54: the user sent message(s) while the model was working."""
    joined = "\n\n".join(t.strip() for t in texts if t.strip())
    return (
        "While you were working, the user sent the following message. Take it into "
        "account and address it as you continue:\n\n" + joined
    )


def stop_hook_cap_warning(cap: int) -> str:
    """01 R6: shown to the user when Stop hooks kept blocking ``cap`` times in a row."""
    return (
        f"Stop hooks blocked the end of this turn {cap} times in a row, so the turn was "
        "ended anyway. Hook authors: check the `stop_hook_active` field of the hook input "
        "and let the turn finish when it is true, to avoid endless continuation."
    )


def output_limit_error_text(limit: int) -> str:
    """01 R28: user-visible error after the recovery nudges are exhausted."""
    return (
        f"The response exceeded the output token limit ({limit} tokens) repeatedly. "
        "Set CLAUDE_CODE_MAX_OUTPUT_TOKENS to raise the limit, or ask for smaller steps."
    )


def thrash_text() -> str:
    """01 R27: the rapid-refill breaker tripped."""
    return (
        "Auto-compaction is thrashing: the context filled up again right after each of "
        "the last compactions. Stopping this turn. Reduce the size of tool outputs or "
        "start a fresh session."
    )


def meta(text: str, origin: str = "auto-continuation") -> Message:
    """Wrap a nudge as a hidden (``is_meta``) user row (01 §2.5)."""
    return Message.user(text, is_meta=True, origin=origin)
