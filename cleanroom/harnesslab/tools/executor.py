"""Streaming concurrency scheduler (WP2). Implements ``interfaces.ToolExecutor``.

Implements spec 01 R9-R12, 03 R9-R15 and 03 §4.1; takeaways 03#T1, 03#T2,
01#T4; P11.

Rules
-----
* Safety is per call from the *parsed* input; parse failure or an exception in
  ``is_concurrency_safe`` = unsafe (03 R9).
* A call may start iff nothing is running, or it and every running call are
  safe; the queue stops at the first unsafe queued call (order preserved).
* Results are emitted walking calls in original order; an unfinished call
  blocks everything after it (01 R10 / 03 §4.1 ``completed_in_order``);
  progress may flush early (03 R11).
* Parallelism cap ``CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`` (default 10).
* Each call gets ``ctx.abort.child()`` (01 R12). Context layers from serial calls
  apply immediately; from safe calls only when the parallel run ends (03 R12).
* ``discard(reason)`` aborts pending calls and synthesises results with
  ``types.SYNTHETIC_RESULT_TEXT[reason]`` (03 R13).
* A denied call causes queued siblings in the batch to get ``SIBLING_DENIED``
  results (01 R52) - design choice: only *unsafe* queued siblings are skipped,
  including unsafe calls that stream in after the denial.
* PostToolBatch hooks fire once per batch (03 R15) - done by the loop after
  ``drain_all`` (WP1), not here.

Design choices
--------------
* One daemon ``threading.Thread`` per running call (the scheduler itself
  enforces the cap), so a tool that ignores its abort signal can never block
  interpreter exit.
* "Applying" a context layer means releasing it through ``context_layers()``
  (and the optional ``on_context_layer`` callback, which WP1 may use to mutate
  the session immediately). Layers of safe calls are held back until no safe
  call is running any more.
* ``discard`` returns every not-yet-emitted result in call order - real results
  of calls that already finished and synthetic ones for the rest - and marks
  them emitted, so the caller can pair every ``tool_use`` without draining.
* ``drain_all`` turns into ``discard(INTERRUPTED)`` when the turn's abort fires
  and calls do not finish within ``ABORT_GRACE_S``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal

from harnesslab.config import env_int
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import ContextLayer, Tool, ToolCallContext, ToolErrorCode
from harnesslab.tools.pipeline import ToolCallOutcome, _error_outcome, run_tool_use, synthetic_outcome
from harnesslab.tools.registry import ToolRegistry, pending_mcp_servers, registry_for
from harnesslab.types import (
    AttachmentEvent,
    LoopEvent,
    Message,
    MessageEvent,
    SyntheticReason,
    ToolProgressEvent,
    ToolUseBlock,
)
from harnesslab.util.abort import AbortController
from harnesslab.util.schema import validate

DEFAULT_MAX_CONCURRENCY = 10
ABORT_GRACE_S = 2.0
WAIT_TICK_S = 0.05


@dataclass
class TrackedCall:
    block: ToolUseBlock
    tool: Tool | None
    safe: bool
    state: Literal["queued", "running", "done"] = "queued"
    outcome: ToolCallOutcome | None = None
    emitted: bool = False
    progress: list[object] = field(default_factory=list)
    abort: AbortController | None = None
    discarded: bool = False
    wait_for_mcp: bool = False


def compute_safety(tool: Tool | None, block: ToolUseBlock) -> bool:
    """03 R9: per-call safety from the parsed input; any doubt = unsafe."""
    if tool is None or block.parse_error is not None:
        return False
    try:
        inp, _ = tool.coerce_input(dict(block.input))
        if validate(inp, tool.input_schema):
            return False
        return bool(tool.is_concurrency_safe(inp))
    except Exception:
        return False


class StreamingToolExecutor:
    """One instance per model round."""

    def __init__(
        self,
        ctx: SessionContext,
        assistant_uuid: str = "",
        *,
        max_concurrency: int | None = None,
        registry: ToolRegistry | None = None,
        runner: Callable[[ToolUseBlock, Tool | None, ToolCallContext], ToolCallOutcome] = run_tool_use,
        on_context_layer: Callable[[ContextLayer], None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.assistant_uuid = assistant_uuid
        self.max_concurrency = max_concurrency or env_int(
            ctx.env, "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", DEFAULT_MAX_CONCURRENCY, lo=1
        )
        self.calls: list[TrackedCall] = []
        self.registry = registry or registry_for(ctx)
        self._runner = runner
        self._on_layer = on_context_layer
        self._cond = threading.Condition(threading.RLock())
        self._released_layers: list[ContextLayer] = []
        self._held_layers: list[ContextLayer] = []
        self.max_running_seen = 0  # observability for tests / metrics
        self._sibling_denied = False

    # ------------------------------------------------------------- queueing
    def add(self, block: ToolUseBlock) -> None:
        """01 R9: queue a completed tool_use immediately and start it if allowed."""
        tool = self.registry.get(block.name)
        waiting = False
        if tool is None and self._mcp_pending(block.name):
            waiting = True  # resolve (with waiting) inside the worker, 03 R1
        call = TrackedCall(block, tool, compute_safety(tool, block), wait_for_mcp=waiting)
        with self._cond:
            self.calls.append(call)
            if tool is None and not waiting:
                # 01 R11: unknown tool -> immediate error result
                call.outcome = self._runner(block, None, self._call_ctx(call))
                call.state = "done"
                self._cond.notify_all()
            elif self._sibling_denied and not call.safe:
                # a sibling in this batch was already denied (01 R52)
                call.outcome = synthetic_outcome(block, SyntheticReason.SIBLING_DENIED, self.assistant_uuid or None)
                call.state = "done"
                self._cond.notify_all()
            self._pump()

    def _mcp_pending(self, name: str) -> bool:
        from harnesslab.tools.registry import mcp_server_of

        server = mcp_server_of(name)
        return server is not None and server in pending_mcp_servers(self.ctx.extra)

    def _call_ctx(self, call: TrackedCall) -> ToolCallContext:
        if call.abort is None:
            call.abort = self.ctx.abort.child()

        def progress(data: object, _call: TrackedCall = call) -> None:
            with self._cond:
                _call.progress.append(data)

        return ToolCallContext(
            session=self.ctx,
            tool_use_id=call.block.id,
            abort=call.abort.signal,
            siblings=[c.block for c in self.calls],
            assistant_uuid=self.assistant_uuid or None,
            progress=progress,
        )

    def _can_start(self, call: TrackedCall, running: list[TrackedCall]) -> bool:
        if len(running) >= self.max_concurrency:
            return False
        return not running or (call.safe and all(r.safe for r in running))

    def _pump(self) -> None:
        """Start queued calls in order; stop at the first one that cannot start."""
        with self._cond:
            for call in self.calls:
                if call.state != "queued":
                    continue
                running = [c for c in self.calls if c.state == "running"]
                if not self._can_start(call, running):
                    break  # preserves order: nothing overtakes a blocked call
                self._start(call)

    def _start(self, call: TrackedCall) -> None:
        call.state = "running"
        call_ctx = self._call_ctx(call)
        self.max_running_seen = max(self.max_running_seen, sum(c.state == "running" for c in self.calls))
        threading.Thread(target=self._work, args=(call, call_ctx), name=f"tool-{call.block.name}", daemon=True).start()

    def _work(self, call: TrackedCall, call_ctx: ToolCallContext) -> None:
        tool = call.tool
        try:
            if tool is None and call.wait_for_mcp:
                tool = self.registry.resolve(
                    call.block.name,
                    pending_servers=lambda: pending_mcp_servers(self.ctx.extra),
                    refresh=lambda: list(self.ctx.tools),
                )
                call.tool = tool
            outcome = self._runner(call.block, tool, call_ctx)
        except BaseException as e:  # never lose a pairing
            outcome = _error_outcome(call.block, call_ctx, f"Internal error while running the tool: {e}", ToolErrorCode.NONE)
        self._finish(call, outcome)

    def _finish(self, call: TrackedCall, outcome: ToolCallOutcome) -> None:
        with self._cond:
            if call.discarded:
                return  # a synthetic result already replaced it
            call.outcome, call.state = outcome, "done"
            # 03 R12: serial layers now; safe layers when the parallel run ends.
            if call.safe:
                self._held_layers.extend(outcome.context_layers)
            else:
                self._release(outcome.context_layers)
            if not any(c.state == "running" and c.safe for c in self.calls):
                held, self._held_layers = self._held_layers, []
                self._release(held)
            if outcome.denied:
                self._sibling_denied = True
                self._skip_unsafe_queued(SyntheticReason.SIBLING_DENIED)
            self._pump()
            self._cond.notify_all()

    def _release(self, layers: list[ContextLayer]) -> None:
        for layer in layers:
            self._released_layers.append(layer)
            if self._on_layer is not None:
                self._on_layer(layer)

    def _skip_unsafe_queued(self, reason: SyntheticReason) -> None:
        for c in self.calls:
            if c.state == "queued" and not c.safe:
                c.outcome = synthetic_outcome(c.block, reason, self.assistant_uuid or None)
                c.state = "done"

    # -------------------------------------------------------------- output
    def _collect_in_order(self) -> list[LoopEvent]:
        events: list[LoopEvent] = []
        with self._cond:
            for c in self.calls:  # 03 R11: progress may flush early
                while c.progress:
                    events.append(ToolProgressEvent(c.block.id, c.progress.pop(0)))
            for c in self.calls:
                if c.state != "done":
                    break  # an unfinished call is a barrier (01 R10)
                if c.emitted or c.outcome is None:
                    continue
                c.emitted = True
                events.append(MessageEvent(c.outcome.result))
                for item in c.outcome.new_messages:
                    events.append(MessageEvent(item) if isinstance(item, Message) else AttachmentEvent(item))  # type: ignore[arg-type]
        return events

    def drain_completed(self) -> Iterator[LoopEvent]:
        yield from self._collect_in_order()

    def drain_all(self) -> Iterator[LoopEvent]:
        """Block until every call finished, yielding results in order as they come.

        On a turn abort, calls get ``ABORT_GRACE_S`` to notice their (child)
        abort signal; whatever is still unfinished then gets an INTERRUPTED
        synthetic result (01 R51)."""
        aborted_at: float | None = None
        while True:
            yield from self._collect_in_order()
            with self._cond:
                if all(c.emitted for c in self.calls):
                    return
                give_up = False
                if self.ctx.abort.signal.aborted:
                    aborted_at = aborted_at or time.monotonic()
                    unfinished = any(c.state != "done" for c in self.calls)
                    give_up = not unfinished or time.monotonic() - aborted_at >= ABORT_GRACE_S
                if not give_up:
                    self._cond.wait(WAIT_TICK_S)
                    continue
            for m in self.discard(SyntheticReason.INTERRUPTED):
                yield MessageEvent(m)
            return

    def discard(self, reason: SyntheticReason) -> list[Message]:
        """03 R13 / 03#T2: abort what is pending; return every unemitted result in
        order (real ones for finished calls, synthetic for the rest)."""
        out: list[Message] = []
        with self._cond:
            for c in self.calls:
                if c.emitted:
                    continue
                if c.state != "done":
                    if c.abort is not None:
                        c.abort.abort(reason.value)
                    c.discarded = c.state == "running"
                    c.outcome = synthetic_outcome(c.block, reason, self.assistant_uuid or None)
                    c.state = "done"
                c.emitted = True
                assert c.outcome is not None
                out.append(c.outcome.result)
            self._cond.notify_all()
        return out

    # ------------------------------------------------------------ queries
    def results(self) -> list[Message]:
        with self._cond:
            return [c.outcome.result for c in self.calls if c.outcome is not None]

    def outcomes(self) -> list[ToolCallOutcome]:
        with self._cond:
            return [c.outcome for c in self.calls if c.outcome is not None]

    def context_layers(self) -> list[ContextLayer]:
        with self._cond:
            return list(self._released_layers)

    def end_turn_request(self) -> str | None:
        """First ``end_turn`` asked for by a finished call (01 R7), if any."""
        with self._cond:
            return next((c.outcome.end_turn for c in self.calls if c.outcome and c.outcome.end_turn), None)

    @property
    def has_pending(self) -> bool:
        with self._cond:
            return any(not c.emitted for c in self.calls)


def partition_runs(blocks: list[ToolUseBlock], safe: list[bool]) -> list[list[int]]:
    """Non-streaming path (03 R14): runs of consecutive safe calls; unsafe singly."""
    runs: list[list[int]] = []
    for i, _ in enumerate(blocks):
        if safe[i] and runs and all(safe[j] for j in runs[-1]):
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs
