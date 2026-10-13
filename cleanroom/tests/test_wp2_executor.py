"""WP2 executor: 03 R9-R14, 01 R9-R12, 03 §4.1 (03#T1, 03#T2)."""

from __future__ import annotations

import threading

import pytest

from harnesslab.tools.base import ContextLayer, ToolOutput
from harnesslab.tools.executor import StreamingToolExecutor, compute_safety, partition_runs
from harnesslab.types import SYNTHETIC_RESULT_TEXT, MessageEvent, SyntheticReason, ToolUseBlock

from tests.wp2_fakes import ConcurrencyProbe, EchoTool, GateTool, allow_all, block, deny_tools, wait_until


def texts(events) -> list[str]:
    return [e.message.tool_results()[0].text() for e in events if isinstance(e, MessageEvent)]


def ids(events) -> list[str]:
    return [e.message.tool_results()[0].tool_use_id for e in events if isinstance(e, MessageEvent)]


@pytest.fixture
def exctx(ctx):
    ctx.can_use_tool = allow_all
    return ctx


def test_safe_calls_run_in_parallel(exctx):
    # Two safe calls must be inside call() at the same time to pass the barrier.
    barrier = threading.Barrier(2, timeout=2)

    class BarrierTool(EchoTool):
        def call(self, input, call_ctx):
            barrier.wait()
            return ToolOutput(data="passed")

    exctx.tools = [BarrierTool("B", safe=True)]
    ex = StreamingToolExecutor(exctx, "asst-1")
    ex.add(block("B", "t1"))
    ex.add(block("B", "t2"))
    assert texts(ex.drain_all()) == ["passed", "passed"]


def test_unsafe_call_blocks_later_calls_and_order_is_kept(exctx):
    serial = GateTool("Serial", safe=False)
    after = EchoTool("After", safe=True)
    exctx.tools = [serial, after]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("Serial", "t1"))
    ex.add(block("After", "t2", text="later"))
    assert serial.started.wait(2)
    assert ex.calls[1].state == "queued"  # did not overtake the serial call
    assert list(ex.drain_completed()) == []  # barrier: nothing emitted yet
    serial.gate.set()
    assert ids(ex.drain_all()) == ["t1", "t2"]
    assert after.calls == [{"text": "later"}]


def test_safe_call_waits_while_unsafe_runs(exctx):
    first = GateTool("Safe1", safe=True)
    serial = GateTool("Serial", safe=False)
    exctx.tools = [first, serial]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("Safe1", "t1"))
    ex.add(block("Serial", "t2"))
    assert first.started.wait(2)
    assert ex.calls[1].state == "queued"  # unsafe cannot join a running safe call
    first.gate.set()
    assert serial.started.wait(2)
    serial.gate.set()
    assert ids(ex.drain_all()) == ["t1", "t2"]


def test_results_emitted_in_original_order(exctx):
    slow = GateTool("Slow", safe=True)
    fast = EchoTool("Fast", safe=True)
    exctx.tools = [slow, fast]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("Slow", "t1"))
    ex.add(block("Fast", "t2", text="fast"))
    assert wait_until(lambda: ex.calls[1].state == "done")
    assert list(ex.drain_completed()) == []  # t2 is done but t1 is not
    slow.gate.set()
    assert ids(ex.drain_all()) == ["t1", "t2"]


def test_unknown_tool_is_an_immediate_error(exctx):
    exctx.tools = []
    ex = StreamingToolExecutor(exctx)
    ex.add(block("Nope", "t1"))
    events = list(ex.drain_completed())
    r = events[0].message.tool_results()[0]
    assert r.is_error and "No such tool available: Nope" in r.text()


def test_parse_failure_or_safety_exception_means_unsafe():
    tool = EchoTool("E", safe=True)
    assert compute_safety(tool, block("E", "t1")) is True
    bad = ToolUseBlock("t2", "E", {}, raw_input='{"text": ', parse_error="truncated")
    assert compute_safety(tool, bad) is False

    class Boom(EchoTool):
        def is_concurrency_safe(self, input):
            raise RuntimeError("cannot tell")

    assert compute_safety(Boom("B"), block("B", "t3")) is False
    assert compute_safety(tool, block("E", "t4", text=5)) is False  # schema failure
    assert compute_safety(None, block("X", "t5")) is False


def test_discard_returns_synthetic_results_and_keeps_pairing(exctx):
    done = EchoTool("Done", safe=True)
    stuck = GateTool("Stuck", safe=True)
    queued = EchoTool("Queued", safe=False)
    exctx.tools = [done, stuck, queued]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("Done", "t1", text="real"))
    ex.add(block("Stuck", "t2"))
    ex.add(block("Queued", "t3"))
    assert wait_until(lambda: ex.calls[0].state == "done")
    msgs = ex.discard(SyntheticReason.STREAMING_FALLBACK)
    assert [m.tool_results()[0].tool_use_id for m in msgs] == ["t1", "t2", "t3"]
    assert msgs[0].tool_results()[0].text() == "real"
    for m in msgs[1:]:
        r = m.tool_results()[0]
        assert r.is_error and SYNTHETIC_RESULT_TEXT[SyntheticReason.STREAMING_FALLBACK] in r.text()
    assert queued.calls == []  # never started
    assert not ex.has_pending
    assert list(ex.drain_all()) == []  # nothing emitted twice
    stuck.gate.set()


def test_concurrency_cap(exctx):
    probe = ConcurrencyProbe(delay=0.1)
    exctx.tools = [probe]
    ex = StreamingToolExecutor(exctx, max_concurrency=2)
    for i in range(5):
        ex.add(block("Probe", f"t{i}"))
    assert len(texts(ex.drain_all())) == 5
    assert probe.peak == 2


def test_concurrency_cap_from_env(exctx):
    exctx.env = {"CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "3"}
    assert StreamingToolExecutor(exctx).max_concurrency == 3
    exctx.env = {}
    assert StreamingToolExecutor(exctx).max_concurrency == 10


def test_context_layers_of_safe_calls_wait_for_the_parallel_run(exctx):
    layer_a = ContextLayer("model", "a")
    a = EchoTool("A", safe=True, layer=layer_a)
    b = GateTool("B", safe=True)
    exctx.tools = [a, b]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("B", "t1"))
    assert b.started.wait(2)
    ex.add(block("A", "t2"))
    assert wait_until(lambda: ex.calls[1].state == "done")
    assert ex.context_layers() == []  # B still running in the same parallel run
    b.gate.set()
    list(ex.drain_all())
    assert ex.context_layers() == [layer_a]


def test_context_layers_of_serial_calls_apply_immediately(exctx):
    layer = ContextLayer("effort", "high")
    serial = EchoTool("S", safe=False, layer=layer)
    later = GateTool("L", safe=False)
    seen: list = []
    exctx.tools = [serial, later]
    ex = StreamingToolExecutor(exctx, on_context_layer=seen.append)
    ex.add(block("S", "t1"))
    ex.add(block("L", "t2"))
    assert later.started.wait(2)
    assert ex.context_layers() == [layer] and seen == [layer]
    later.gate.set()
    list(ex.drain_all())


def test_denied_call_skips_unsafe_queued_siblings(exctx):
    exctx.can_use_tool = deny_tools("W")
    w = EchoTool("W", safe=False)
    x = EchoTool("X", safe=False)
    exctx.tools = [w, x]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("W", "t1"))
    ex.add(block("X", "t2"))
    out = texts(ex.drain_all())
    assert "did NOT happen" in out[0]
    assert SYNTHETIC_RESULT_TEXT[SyntheticReason.SIBLING_DENIED] in out[1]
    assert x.calls == []


def test_abort_during_drain_yields_interrupted_results(exctx):
    g = GateTool("G", safe=True)
    exctx.tools = [g]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("G", "t1"))
    assert g.started.wait(2)
    exctx.abort.abort("user interrupt")
    out = list(ex.drain_all())
    r = out[0].message.tool_results()[0]
    assert r.is_error and SYNTHETIC_RESULT_TEXT[SyntheticReason.INTERRUPTED] in r.text()


def test_each_call_gets_a_child_abort(exctx):
    seen = []

    class Grab(EchoTool):
        def call(self, input, call_ctx):
            seen.append(call_ctx.abort)
            return ToolOutput(data="x")

    exctx.tools = [Grab("G")]
    ex = StreamingToolExecutor(exctx)
    ex.add(block("G", "t1"))
    ex.add(block("G", "t2"))
    list(ex.drain_all())
    assert len({id(s) for s in seen}) == 2
    assert all(s is not exctx.abort.signal for s in seen)


def test_results_carry_assistant_uuid(exctx):
    exctx.tools = [EchoTool("E")]
    ex = StreamingToolExecutor(exctx, "asst-9")
    ex.add(block("E", "t1"))
    (ev,) = list(ex.drain_all())
    assert ev.message.source_tool_assistant_uuid == "asst-9"
    assert ex.results()[0] is ev.message


def test_mcp_tool_waits_for_connecting_server(exctx):
    class Late(EchoTool):
        is_mcp = True

    pending = {"srv"}
    exctx.extra["mcp_pending_servers"] = lambda: pending
    exctx.tools = []
    ex = StreamingToolExecutor(exctx)
    ex.add(block("mcp__srv__ping", "t1", text="pong"))

    def connect():
        exctx.tools.append(Late("mcp__srv__ping"))
        pending.clear()

    threading.Timer(0.1, connect).start()
    assert texts(ex.drain_all()) == ["pong"]


def test_partition_runs():
    blocks = [ToolUseBlock(f"t{i}", "X") for i in range(6)]
    assert partition_runs(blocks, [True, True, False, True, False, False]) == [[0, 1], [2], [3], [4], [5]]
    assert partition_runs([], []) == []
