"""The agent turn loop: one generator, named transitions (WP1).

Implements spec 01 R1-R12, R26-R33, R51-R54 and 01 §4 ``run_query``;
takeaways 01#T1 (generator loop with explicit transitions), 01#T2 (errors as
withheld messages), 01#T4 (stream-time tool start), P01/P02.

Shape (one iteration = one model round, 01 R2)::

    1  fold queued commands / notifications / mid-turn input / attachments
    2  proactive auto-compaction (deps.compactor.maybe_autocompact)
    3  blocking-limit check (-> TerminalReason.BLOCKING_LIMIT)
    4  build request (deps.build_request: model, effort, reminders, caching)
    5  yield StreamRequestStart
    6  stream via deps.call_model; feed StreamAccumulator; each completed
       ToolUseBlock -> executor.add(); periodically yield executor.drain_completed()
    7  recovery ladder (loop.recovery.recovery_ladder) -> Continue | Terminal | None
    8  no tool use -> Stop hooks (block -> STOP_HOOK_BLOCKING, cap 8 -> COMPLETED)
    9  drain all tool results, PostToolBatch hooks, fold mid-turn user input
   10  max-turns check (turn_count + 1 > max_turns)
   11  state = state.next(NEXT_TURN, ...)

The generator's *return value* is the ``TerminalReason``; it also yields a final
``TurnEndEvent`` so consumers that only iterate still learn why it ended.

Design choices (spec silent): synchronous generator + worker threads (instead
of asyncio) for readability; abort is cooperative via ``ctx.abort``.

More design choices:

* One assistant row per model response, yielded when the stream ends. Tools
  still *start* the moment their block completes (01 R9 MUST); tool results
  finished during streaming are buffered and yielded right after their
  assistant row, so consumers and the transcript always see a tool_use before
  its result (01 R9 "SHOULD emit during streaming" is relaxed to that).
  ``ToolProgressEvent``s pass through immediately.
* The budget check (01 R8, architect G14) runs at the top of every iteration:
  ``TerminalReason.BUDGET_EXCEEDED``. The remaining-budget attachment itself is
  produced by WP4's attachment pipeline.
* The final item list is published as ``ctx.extra["loop_items"]`` so the
  ``session.Engine`` can keep its history after compaction rewrote it.
* Tool-initiated turn ends (01 R7) come from ``executor.end_turn_request()``
  when the executor has it (the real one does), else duck-typed from
  ``executor.calls[i].outcome.end_turn`` (the FINAL protocol has no accessor).
* After a tool batch: ``tools.output.enforce_batch_budget`` over the whole
  batch, then released context layers are applied (``loop.context_layers``).
* Integration seams published in ``ctx.extra`` each round (see
  ``publish_items`` / ``publish_request_params``): ``conversation_items``,
  ``current_items``, ``cache_safe_params``, ``prefix_tokens``,
  ``rendered_system_prompt``. After a tool batch the loop honours
  ``ctx.extra["end_turn_request"]`` set by the permission layer (04 R22-R23)
  by recording a system row and ending ``COMPLETED`` (spec names no reason).
* Hook payloads are built here (session_id, transcript_path, cwd,
  hook_event_name + event fields, 04 §2.8) so the loop does not depend on WP3.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Generator, Iterator

from harnesslab.api.accounting import CostTracker
from harnesslab.api.request import blocking_limit, model_info, resolve_max_tokens
from harnesslab.api.retry import AttemptAbandoned, PartialStreamKept
from harnesslab.hooks.types import HookEvent
from harnesslab.interfaces import ToolExecutor, TurnInfo
from harnesslab.loop import nudges
from harnesslab.loop.context_layers import apply_layers
from harnesslab.loop.recovery import recovery_ladder
from harnesslab.loop.state import Continue, LoopState, QueryDeps, QueryParams, Terminal
from harnesslab.model.base import (
    APIError,
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    Ping,
    StreamAccumulator,
)
from harnesslab.runtime import SessionContext
from harnesslab.tokens import context_tokens
from harnesslab.types import (
    ATTACHMENT_RENDERERS,
    ApiErrorDetail,
    Attachment,
    AttachmentEvent,
    ConversationItem,
    LoopEvent,
    Message,
    MessageEvent,
    RawStreamEvent,
    StreamRequestStart,
    SyntheticReason,
    SystemNoticeEvent,
    TerminalReason,
    TombstoneEvent,
    ToolUseBlock,
    TransitionReason,
    TurnEndEvent,
    new_uuid,
)
from harnesslab.util.abort import AbortedError
from harnesslab.util.text import wrap_system_reminder

log = logging.getLogger(__name__)

_RAW_EVENTS = (MessageStart, ContentBlockStart, ContentBlockDelta, ContentBlockStop, MessageDelta, MessageStop, Ping)
MIDTURN_ATTACHMENT = "midturn_user_input"
MAX_TURNS_ATTACHMENT = "max_turns_reached"

# Own renderers for the attachment types this module creates (WP4 may override).
ATTACHMENT_RENDERERS.setdefault(MIDTURN_ATTACHMENT, lambda p: nudges.midturn_input_text(list(p.get("texts", []))))
ATTACHMENT_RENDERERS.setdefault(
    MAX_TURNS_ATTACHMENT,
    lambda p: f"The maximum number of turns ({p.get('max_turns')}) was reached; the run stopped here.",
)


# ------------------------------------------------------------------ wiring


def default_deps(ctx: SessionContext) -> QueryDeps:
    """Wire the real subsystems: api.retry.call_with_retries, api.request.build_model_request,
    tools.executor.StreamingToolExecutor, context.attachments.AttachmentPipeline,
    compaction.controller.AutoCompactor, ctx.hook_runner, context.prompt.SystemPromptBuilder.

    Imports MUST be local to this function (other WPs' modules), so that importing
    ``loop.query`` never fails while siblings are unimplemented."""
    from harnesslab.api.request import build_model_request
    from harnesslab.api.retry import call_with_retries
    from harnesslab.compaction.controller import AutoCompactor
    from harnesslab.context.attachments import AttachmentPipeline
    from harnesslab.context.prompt import SystemPromptBuilder
    from harnesslab.tools.executor import StreamingToolExecutor

    info = model_info(ctx.model_id, ctx.env)
    prompt = SystemPromptBuilder()
    return QueryDeps(
        call_model=call_with_retries,
        build_request=functools.partial(build_model_request, system_prompt=prompt),
        make_executor=lambda c, assistant_uuid: StreamingToolExecutor(c, assistant_uuid),
        attachments=AttachmentPipeline(),
        compactor=AutoCompactor(max_output=resolve_max_tokens(info, ctx.env), context_window=info.context_window),
        hooks=ctx.hook_runner,
        system_prompt=prompt,
    )


def cost_tracker(ctx: SessionContext) -> CostTracker:
    """Session-wide tracker in ``ctx.extra["cost_tracker"]`` (01 R50)."""
    t = ctx.extra.get("cost_tracker")
    if not isinstance(t, CostTracker):
        t = CostTracker(total_usd=ctx.cost_usd, us_geo=bool(ctx.extra.get("us_geo")))
        ctx.extra["cost_tracker"] = t
    return t


def _hooks(ctx: SessionContext, deps: QueryDeps) -> Any:
    return deps.hooks if deps.hooks is not None else ctx.hook_runner


def hook_payload(event: HookEvent, ctx: SessionContext, **fields: Any) -> dict[str, Any]:
    """04 §2.8 common fields + event fields."""
    return {
        "session_id": ctx.session_id,
        "transcript_path": str(ctx.transcript_path),
        "cwd": str(ctx.cwd),
        "permission_mode": ctx.permissions.mode.value,
        "hook_event_name": event.value,
        **fields,
    }


def _enforce_batch_budget(batch: list[Message], ctx: SessionContext, assistant: Message) -> None:
    """03 R25 via ``tools.output.enforce_batch_budget`` (WP2), imported lazily."""
    if not batch:
        return
    from harnesslab.tools.output import enforce_batch_budget

    by_name = {getattr(t, "name", ""): t for t in ctx.tools}
    tools_by_id = {b.id: by_name[b.name] for b in assistant.tool_uses() if b.name in by_name}
    enforce_batch_budget(batch, ctx, tools_by_id)


def publish_items(ctx: SessionContext, items: list[ConversationItem]) -> None:
    """Integration seam: the current item list for readers outside the loop -
    the auto-mode classifier projection and Stop prompt hooks read
    ``conversation_items`` (04 §4.5), ``AgentTool`` builds forks from
    ``current_items`` (06 §4.3)."""
    ctx.extra["conversation_items"] = items
    ctx.extra["current_items"] = items


def publish_request_params(ctx: SessionContext, request: Any) -> None:
    """Integration seam: what the compaction summarizer needs to fork warm
    (``cache_safe_params``, 05 R17), the prefix estimate for the
    prefix-too-large breaker (``prefix_tokens``, 05 R12) and, on the main
    thread, the rendered system prompt a fork reuses byte for byte (06 R26)."""
    from harnesslab.tokens import estimate_text_tokens

    ctx.extra["cache_safe_params"] = {"system": list(request.system), "tools": list(request.tools or [])}
    prefix = sum(estimate_text_tokens(getattr(b, "text", "") or "") for b in request.system)
    prefix += sum(estimate_text_tokens(str(t), is_json=True) for t in request.tools or [])
    ctx.extra["prefix_tokens"] = prefix
    if not ctx.is_fork:
        ctx.extra["rendered_system_prompt"] = list(request.system)


def _record(ctx: SessionContext, item: ConversationItem) -> None:
    if ctx.transcript is not None:
        ctx.transcript.append(item)


def _event(item: ConversationItem) -> LoopEvent:
    return AttachmentEvent(item) if isinstance(item, Attachment) else MessageEvent(item)


def _reminder(text: str, origin: str) -> Message:
    return Message.user(wrap_system_reminder(text), is_meta=True, origin=origin)


# ------------------------------------------------------------------ steps


def fold_pending_inputs(state: LoopState, ctx: SessionContext, deps: QueryDeps) -> list[ConversationItem]:
    """Step 1: drain ``ctx.notifications`` (06 §4.4), wrap ``ctx.pending_user_inputs``
    as a mid-turn attachment (01 R54), then append ``deps.attachments.collect(...)``."""
    out: list[ConversationItem] = []
    if ctx.notifications is not None:
        out.extend(ctx.notifications.drain())
    out.extend(take_midturn_input(ctx))
    if deps.attachments is not None:
        items = state.items + out
        prompt = None
        for it in reversed(items):
            if isinstance(it, Message) and it.role == "user" and not it.is_meta and not it.tool_results():
                prompt = it.text()
                break
        info = TurnInfo(
            items=items,
            turn_count=state.turn_count,
            is_first_turn=state.turn_count == 0 and state.transition is None,
            user_prompt=prompt,
            tokens_used=context_tokens(items),
        )
        out.extend(deps.attachments.collect(ctx, info))
    return out


def take_midturn_input(ctx: SessionContext) -> list[ConversationItem]:
    """01 R54: user text typed while the model worked becomes one attachment."""
    if not ctx.pending_user_inputs:
        return []
    texts, ctx.pending_user_inputs[:] = list(ctx.pending_user_inputs), []
    return [Attachment(MIDTURN_ATTACHMENT, {"texts": texts})]


def run_stop_hooks(state: LoopState, assistant: Message, ctx: SessionContext, deps: QueryDeps) -> tuple[bool, list[ConversationItem]]:
    """Step 8 (01 R6): returns ``(blocking, items_to_append)``. Input payload must include
    ``stop_hook_active`` (True once a previous Stop hook blocked in this turn)."""
    runner = _hooks(ctx, deps)
    if runner is None:
        return False, []
    event = HookEvent.SUBAGENT_STOP if ctx.agent_id is not None else HookEvent.STOP
    payload = hook_payload(
        event, ctx,
        stop_hook_active=state.guards.stop_hook_block_count > 0,
        last_assistant_message=assistant.text(),
    )
    res = runner.run(event, payload, ctx)
    extra: list[ConversationItem] = []
    if res.blocking_errors:
        text = "A Stop hook asked you to keep working:\n" + "\n".join(res.blocking_errors)
        extra.append(_reminder(text, "hook"))
        return True, extra
    if res.prevent_continuation:
        ctx.extra["stop_prevented"] = res.stop_reason or "stopped by hook"
    return False, extra


def over_max_turns(state: LoopState, params: QueryParams) -> bool:
    """01 R5: ``turn_count + 1 > max_turns``.

    The spec counts rounds from 1; ``LoopState.turn_count`` (FINAL) counts
    completed ``next_turn`` transitions from 0. So the spec's ``turn_count`` is
    ``state.turn_count + 1`` here, and ``max_turns=1`` allows exactly one model
    round."""
    return params.max_turns is not None and (state.turn_count + 1) + 1 > params.max_turns


def interruption_items(during_tools: bool, executor_results: list[Message]) -> list[ConversationItem]:
    """01 R51: synthetic results for pending tools + an interruption user message
    (variants for "during the response" vs "during tool use")."""
    return [*executor_results, Message.user(nudges.interruption_text(during_tools), origin="interrupt")]


def _tool_end_turn(executor: ToolExecutor) -> TerminalReason | None:
    """01 R7: a tool may end / defer the turn or stop continuation."""
    mapping = {
        "completed": TerminalReason.COMPLETED,
        "tool_deferred": TerminalReason.TOOL_DEFERRED,
        "hook_stopped": TerminalReason.HOOK_STOPPED,
    }
    accessor = getattr(executor, "end_turn_request", None)  # StreamingToolExecutor (WP2)
    if callable(accessor):
        return mapping.get(accessor() or "")
    for call in getattr(executor, "calls", []) or []:
        end = getattr(getattr(call, "outcome", None), "end_turn", None)
        if end in mapping:
            return mapping[end]
    return None


def _is_blocked(items: list[ConversationItem], ctx: SessionContext, model_id: str) -> bool:
    """Step 3 (01 R49, 05 §4.1 block level)."""
    info = model_info(model_id, ctx.env)
    return context_tokens(items) >= blocking_limit(info, ctx.env, resolve_max_tokens(info, ctx.env))


def _partial_message(acc: StreamAccumulator, uuid: str) -> Message:
    """What survived a cut stream: completed blocks plus streamed text/thinking;
    tool calls whose JSON never finished are dropped (they were never dispatched)."""
    msg = acc.message()
    completed = {id(b) for b in acc.completed}
    msg.content = [b for b in msg.content if not isinstance(b, ToolUseBlock) or id(b) in completed]
    msg.uuid = uuid
    return msg


# ------------------------------------------------------------------ the loop


class _Round:
    """Mutable per-round streaming state (one model call, possibly several attempts)."""

    def __init__(self, ctx: SessionContext, deps: QueryDeps) -> None:
        self.ctx, self.deps = ctx, deps
        self.reset()

    def reset(self) -> None:
        self.assistant_uuid = new_uuid()
        self.executor: ToolExecutor = self.deps.make_executor(self.ctx, self.assistant_uuid)
        self.acc = StreamAccumulator()
        self.buffered: list[Message] = []  # tool results finished during streaming
        self.dispatched = 0

    def pump(self) -> Iterator[LoopEvent]:
        """Collect finished results (buffered) and pass progress through."""
        for ev in self.executor.drain_completed():
            if isinstance(ev, MessageEvent):
                self.buffered.append(ev.message)
            else:
                yield ev


def run_query(
    items: list[ConversationItem],
    ctx: SessionContext,
    params: QueryParams | None = None,
    deps: QueryDeps | None = None,
) -> Generator[LoopEvent, None, TerminalReason]:
    """Drive one user turn to completion (01 R1, R2). ``items`` already contains
    the new user message. Every row produced is yielded *and* appended to the
    session transcript if ``ctx.transcript`` is set."""
    params = params or QueryParams(
        max_turns=ctx.max_turns,
        max_budget_usd=ctx.max_budget_usd,
        is_subagent=ctx.agent_id is not None,
        is_interactive=ctx.is_interactive,
        query_source="main" if ctx.agent_id is None else "subagent",
        keep_partial_on_abort=ctx.is_sdk,
    )
    deps = deps or default_deps(ctx)
    tracker = cost_tracker(ctx)
    state = LoopState(items=list(items))
    model_override: str | None = None

    def end(reason: TerminalReason) -> Generator[LoopEvent, None, TerminalReason]:
        ctx.extra["loop_items"] = state.items
        log.info("turn ended: %s after %d round(s)", reason.value, state.turn_count + 1)
        yield TurnEndEvent(reason, state.turn_count)
        return reason

    def emit(item: ConversationItem) -> LoopEvent:
        _record(ctx, item)
        return _event(item)

    while True:
        # budget (01 R8, G14)
        if tracker.over_budget(params.max_budget_usd):
            yield SystemNoticeEvent("budget_exceeded", f"The USD budget of {params.max_budget_usd} was used up.")
            return (yield from end(TerminalReason.BUDGET_EXCEEDED))

        # 1. fold inputs
        for it in fold_pending_inputs(state, ctx, deps):
            state.items.append(it)
            yield emit(it)

        # 2. proactive compaction
        if deps.compactor is not None:
            res = deps.compactor.maybe_autocompact(state.items, ctx)
            if res is not None:
                old = {id(x) for x in state.items}
                state.items = list(res.items)
                yield SystemNoticeEvent(
                    "compact_boundary", "Conversation compacted.",
                    {"trigger": res.trigger, "pre_tokens": res.pre_tokens, "post_tokens": res.post_tokens},
                )
                for it in state.items:
                    if id(it) not in old:
                        yield emit(it)
            if deps.compactor.thrashing(ctx):  # 01 R27
                yield emit(Message.system(nudges.thrash_text()))
                return (yield from end(TerminalReason.RAPID_REFILL_BREAKER))

        # 3. blocking limit (reactive compaction may still rescue the request)
        model_id = model_override or state.model_override or ctx.model_id
        reactive_available = deps.compactor is not None and not state.guards.tried_reactive_compact
        if _is_blocked(state.items, ctx, model_id) and not reactive_available:
            return (yield from end(TerminalReason.BLOCKING_LIMIT))

        # 4-5. request
        if model_override:
            state.model_override = model_override
        elif ctx.extra.get("layer_model"):  # a Skill's model layer (07 R8)
            state.model_override = ctx.extra["layer_model"]
        publish_items(ctx, state.items)
        request = deps.build_request(state, ctx, params)
        ctx.extra["last_max_tokens"] = request.max_tokens
        publish_request_params(ctx, request)
        yield StreamRequestStart(request.model)

        # 6. stream + dispatch tools
        rnd = _Round(ctx, deps)
        assistant: Message | None = None
        try:
            for ev in deps.call_model(request, ctx):
                if ctx.abort.signal.aborted:
                    raise AbortedError(ctx.abort.signal.reason or "aborted")
                if isinstance(ev, AttemptAbandoned):  # 01 R16: retract and start over
                    yield TombstoneEvent(rnd.assistant_uuid)
                    rnd.executor.discard(SyntheticReason.STREAMING_FALLBACK)
                    rnd.reset()
                    continue
                if isinstance(ev, _RAW_EVENTS):
                    yield RawStreamEvent(ev)
                    for block in rnd.acc.feed(ev):
                        if isinstance(block, ToolUseBlock):
                            rnd.executor.add(block)  # 01 R9: start as soon as it completes
                            rnd.dispatched += 1
                    yield from rnd.pump()
                    continue
                if isinstance(ev, SystemNoticeEvent) and ev.subtype == "model_fallback":
                    model_override = ev.data.get("to") or model_override  # 01 R33/R35
                yield ev
            assistant = rnd.acc.message()
            assistant.uuid = rnd.assistant_uuid
        except PartialStreamKept as kept:
            assistant = _partial_message(rnd.acc, rnd.assistant_uuid)
            e = kept.error
            assistant.api_error = ApiErrorDetail(e.kind, e.message, e.status, e.actual_tokens, e.limit_tokens)
        except APIError as e:
            if rnd.acc.started:
                yield TombstoneEvent(rnd.assistant_uuid)
            rnd.executor.discard(SyntheticReason.STREAMING_FALLBACK)
            assistant = Message.assistant(
                f"API error: {e.message}",
                is_api_error=True,
                api_error=ApiErrorDetail(e.kind, e.message, e.status, e.actual_tokens, e.limit_tokens),
                model=request.model,
            )
            rnd.dispatched = 0
        except AbortedError:
            # 01 R51 / R53: abort during the response
            partial = _partial_message(rnd.acc, rnd.assistant_uuid)
            synthetic = rnd.executor.discard(SyntheticReason.INTERRUPTED)
            keep = params.keep_partial_on_abort and partial.content
            if keep:
                partial.aborted_mid_stream = True
                state.items.append(partial)
                yield emit(partial)
                for m in rnd.buffered + synthetic:
                    state.items.append(m)
                    yield emit(m)
            elif rnd.acc.started:
                yield TombstoneEvent(rnd.assistant_uuid)
            note = interruption_items(False, [])
            for it in note:
                state.items.append(it)
                yield emit(it)
            return (yield from end(TerminalReason.ABORTED_STREAMING))

        if assistant.usage is not None and not assistant.is_api_error:
            tracker.add(assistant.usage, assistant.model or request.model)  # final usage (01 R50)
            ctx.cost_usd = tracker.total_usd

        # 7. recovery ladder
        outcome = recovery_ladder(state, assistant, ctx, params, deps.compactor)
        if isinstance(outcome, Continue):
            appended: list[ConversationItem] = []
            if outcome.keep_assistant and not assistant.is_api_error:
                appended.append(assistant)
                yield emit(assistant)
                if rnd.dispatched:  # keep real results for tools that already started
                    results = rnd.buffered + [ev.message for ev in rnd.executor.drain_all() if isinstance(ev, MessageEvent)]
                    for m in results:
                        appended.append(m)
                        yield emit(m)
            else:
                if outcome.tombstone is not None or rnd.acc.started:
                    yield TombstoneEvent(assistant.uuid)
                if rnd.dispatched:
                    rnd.executor.discard(SyntheticReason.STREAMING_FALLBACK)
            if outcome.replace_items is not None:
                # 05 R22/R26: every row of a CompactResult (fresh boundary, summary,
                # copied kept messages, re-injected attachments) goes to the transcript.
                old_ids = {id(x) for x in state.items}
                yield SystemNoticeEvent("compact_boundary", "Conversation compacted after the prompt was too long.", {"trigger": "reactive"})
                for it in outcome.replace_items:
                    if id(it) not in old_ids:
                        yield emit(it)
            for it in outcome.extra:
                yield emit(it)
            nxt = state.next(outcome.reason, append=appended + list(outcome.extra))
            if outcome.replace_items is not None:
                nxt.items = list(outcome.replace_items) + list(outcome.extra)
            state = nxt
            continue
        if isinstance(outcome, Terminal):
            if not assistant.is_api_error and assistant.content:
                state.items.append(assistant)
                yield emit(assistant)
                if rnd.dispatched:
                    for ev in rnd.executor.drain_all():
                        if isinstance(ev, MessageEvent):
                            state.items.append(ev.message)
                            yield emit(ev.message)
            if outcome.surface is not None:
                state.items.append(outcome.surface)
                yield emit(outcome.surface)
            return (yield from end(outcome.reason))

        # normal end of stream: show the assistant row
        state.items.append(assistant)
        yield emit(assistant)

        # 8. stop hooks when no tool was used
        if not assistant.has_tool_use():
            blocking, extra = run_stop_hooks(state, assistant, ctx, deps)
            if blocking:
                state.guards.stop_hook_block_count += 1
                if state.guards.stop_hook_block_count >= params.stop_hook_block_cap:
                    yield SystemNoticeEvent("stop_hook_cap", nudges.stop_hook_cap_warning(params.stop_hook_block_cap))
                    return (yield from end(TerminalReason.COMPLETED))
                for it in extra:
                    yield emit(it)
                state = state.next(TransitionReason.STOP_HOOK_BLOCKING, append=extra)
                if over_max_turns(state, params):  # 01 R5: re-check when a hook keeps the turn alive
                    yield from _max_turns_attachment(state, params, emit)
                    return (yield from end(TerminalReason.MAX_TURNS))
                continue
            if ctx.extra.pop("stop_prevented", None) is not None:
                return (yield from end(TerminalReason.STOP_HOOK_PREVENTED))
            return (yield from end(TerminalReason.COMPLETED))

        # 9. drain tool results; the per-message budget (03 R25) is applied to the
        #    whole batch before any of it is recorded, so the transcript and the
        #    model see the same (possibly spilled) blocks
        batch = list(rnd.buffered)
        for ev in rnd.executor.drain_all():
            if isinstance(ev, MessageEvent):
                batch.append(ev.message)
            else:
                yield ev
        _enforce_batch_budget(batch, ctx, assistant)
        for m in batch:
            state.items.append(m)
            yield emit(m)
        apply_layers(ctx, rnd.executor.context_layers())  # 03 R12 / 07 R8
        if ctx.abort.signal.aborted:  # 01 R51: abort during tool use
            for it in interruption_items(True, rnd.executor.discard(SyntheticReason.INTERRUPTED)):
                state.items.append(it)
                yield emit(it)
            return (yield from end(TerminalReason.ABORTED_TOOLS))

        runner = _hooks(ctx, deps)
        if runner is not None:  # 03 R15: once per batch
            calls = [{"tool_name": b.name, "tool_use_id": b.id, "tool_input": b.input} for b in assistant.tool_uses()]
            res = runner.run(HookEvent.POST_TOOL_BATCH, hook_payload(HookEvent.POST_TOOL_BATCH, ctx, tool_calls=calls), ctx)
            notes = list(res.additional_contexts) + list(res.blocking_errors)
            if notes:
                hook_note = _reminder("\n".join(notes), "hook")
                state.items.append(hook_note)
                yield emit(hook_note)
            if res.prevent_continuation:
                return (yield from end(TerminalReason.HOOK_STOPPED))

        ended = _tool_end_turn(rnd.executor)
        if ended is not None:
            return (yield from end(ended))

        permission_stop = ctx.extra.pop("end_turn_request", None)
        if permission_stop:  # 04 R22/R23: auto mode unavailable / headless denial limit
            note = Message.system(str(permission_stop), subtype="permission_end_turn")
            state.items.append(note)
            yield emit(note)
            yield SystemNoticeEvent("permission_end_turn", str(permission_stop))
            return (yield from end(TerminalReason.COMPLETED))

        for it in take_midturn_input(ctx):  # 01 R54
            state.items.append(it)
            yield emit(it)

        # 10. max turns
        if over_max_turns(state, params):
            yield from _max_turns_attachment(state, params, emit)
            return (yield from end(TerminalReason.MAX_TURNS))

        # 11. next round
        state = state.next(TransitionReason.NEXT_TURN)


def _max_turns_attachment(state: LoopState, params: QueryParams, emit: Any) -> Iterator[LoopEvent]:
    att = Attachment(MAX_TURNS_ATTACHMENT, {"max_turns": params.max_turns, "turn_count": state.turn_count + 1})
    state.items.append(att)
    yield emit(att)


__all__ = [
    "run_query", "default_deps", "fold_pending_inputs", "run_stop_hooks", "over_max_turns",
    "interruption_items", "take_midturn_input", "cost_tracker", "hook_payload",
]
