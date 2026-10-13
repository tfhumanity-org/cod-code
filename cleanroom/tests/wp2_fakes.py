"""Shared fakes for WP2 tests: tiny tools and permission stand-ins.

Nothing here depends on another work package: permissions are lambdas, hooks
are a stub runner, and tools are plain ``Tool`` subclasses.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from harnesslab.hooks.types import AggregatedHookResult
from harnesslab.permissions.types import Decision
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import ContextLayer, Tool, ToolCallContext, ToolOutput
from harnesslab.types import ToolUseBlock
from harnesslab.util.abort import AbortController


def allow_all(tool: Tool, inp: dict, call_ctx: ToolCallContext) -> Decision:
    return Decision.allow()


def deny_tools(*names: str, message: str = "blocked by test") -> Callable[..., Decision]:
    def can(tool: Tool, inp: dict, call_ctx: ToolCallContext) -> Decision:
        return Decision.deny(message) if tool.name in names else Decision.allow()

    return can


def call_ctx_for(ctx: SessionContext, tool_use_id: str = "tu_1", siblings: list[ToolUseBlock] | None = None) -> ToolCallContext:
    return ToolCallContext(session=ctx, tool_use_id=tool_use_id, abort=AbortController().signal, siblings=siblings or [])


class EchoTool(Tool):
    """Returns its ``text`` input; safety configurable per instance."""

    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False}

    def __init__(self, name: str = "Echo", *, safe: bool = True, delay: float = 0.0, layer: ContextLayer | None = None) -> None:
        self.name = name  # type: ignore[misc]
        self.safe = safe
        self.delay = delay
        self.layer = layer
        self.calls: list[dict] = []

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return self.safe

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        self.calls.append(input)
        if self.delay:
            time.sleep(self.delay)
        return ToolOutput(data=input.get("text", "ok"), context_layers=[self.layer] if self.layer else [])


class GateTool(Tool):
    """Blocks until its gate event is set (or its abort fires)."""

    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self, name: str = "Gate", *, safe: bool = True, layer: ContextLayer | None = None) -> None:
        self.name = name  # type: ignore[misc]
        self.safe = safe
        self.gate = threading.Event()
        self.started = threading.Event()
        self.layer = layer

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return self.safe

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        self.started.set()
        while not self.gate.wait(0.01):
            call_ctx.abort.raise_if_aborted()
        return ToolOutput(data=input.get("text", self.name), context_layers=[self.layer] if self.layer else [])


class ConcurrencyProbe(Tool):
    """Safe tool that records the maximum number of simultaneous calls."""

    name = "Probe"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return ToolOutput(data="probe")


class StubHooks:
    """``HookRunner`` stand-in returning a canned aggregate per event."""

    def __init__(self, results: dict | None = None) -> None:
        self.results = results or {}
        self.seen: list[tuple[Any, dict]] = []

    def run(self, event, payload, ctx) -> AggregatedHookResult:
        self.seen.append((event, payload))
        return self.results.get(event, AggregatedHookResult())


def block(name: str, id: str, **inp: Any) -> ToolUseBlock:
    return ToolUseBlock(id=id, name=name, input=inp)


def wait_until(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()
