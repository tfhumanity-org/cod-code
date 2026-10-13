"""Architect smoke tests: every module imports, every module cites the spec, and
the FINAL shared modules behave. Must stay green at every merge."""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import harnesslab
from harnesslab.agents.notifications import NotificationQueue, render_task_notification
from harnesslab.agents.tasks import TaskRegistry
from harnesslab.config import Settings, SettingsSource, env_int
from harnesslab.model.base import APIError, StreamAccumulator
from harnesslab.model.scripted import ScriptedModel, ScriptExhausted, call, fail, say, turn
from harnesslab.model.base import ModelRequest
from harnesslab.loop.state import LoopState, TransitionReason
from harnesslab.tokens import context_tokens, estimate_text_tokens
from harnesslab.transcript.rows import item_to_row, row_to_item
from harnesslab.types import (
    ApiErrorDetail,
    Attachment,
    ErrorKind,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from harnesslab.util.abort import AbortController
from harnesslab.util.frontmatter import parse_frontmatter
from harnesslab.util.schema import validate
from harnesslab.util.text import head, wrap_system_reminder

ALL_MODULES = sorted(
    m.name for m in pkgutil.walk_packages(harnesslab.__path__, prefix="harnesslab.")
)
SPEC_REF = re.compile(r"(\b0\d\b|\b10\b|\bR\d+|#T\d+|§\d|\bP\d\d\b|spec)")


def test_module_inventory_is_complete():
    """Every .py file under harnesslab/ is reachable by walk_packages (no missing __init__)."""
    root = Path(harnesslab.__file__).parent
    on_disk = set()
    for p in root.rglob("*.py"):
        parts = p.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            on_disk.add("harnesslab." + ".".join(parts))
    assert on_disk == set(ALL_MODULES)


@pytest.mark.parametrize("name", ALL_MODULES)
def test_every_module_imports_and_cites_spec(name):
    mod = importlib.import_module(name)
    doc = mod.__doc__ or ""
    assert doc.strip(), f"{name} has no module docstring"
    assert SPEC_REF.search(doc), f"{name} docstring cites no spec requirement"


# ------------------------------------------------------------------ FINAL modules


def _run(model, request=None):
    acc = StreamAccumulator()
    completed = []
    for ev in model.stream(request or ModelRequest(model="m")):
        completed += acc.feed(ev)
    return acc, completed


def test_scripted_model_streams_text_and_tool_use():
    m = ScriptedModel([turn("reading", call("Read", {"file_path": "/x"}, id="t1")), say("done")])
    acc, completed = _run(m)
    msg = acc.message()
    assert msg.text() == "reading"
    assert [b.name for b in msg.tool_uses()] == ["Read"]
    assert msg.stop_reason == "tool_use"
    assert isinstance(completed[-1], ToolUseBlock) and completed[-1].input == {"file_path": "/x"}
    acc2, _ = _run(m)
    assert acc2.message().text() == "done" and acc2.message().stop_reason == "end_turn"
    with pytest.raises(ScriptExhausted):
        _run(m)
    assert len(m.requests) == 3


def test_scripted_model_errors_and_malformed_json():
    err = APIError(ErrorKind.OVERLOADED, status=529)
    m = ScriptedModel([fail(err), fail(err, after_events=3)])
    with pytest.raises(APIError):
        _run(m)
    acc = StreamAccumulator()
    with pytest.raises(APIError):
        for ev in m.stream(ModelRequest(model="m")):
            acc.feed(ev)
    assert acc.progress in ("started", "real_output")

    bad = ScriptedModel([turn(call("Bash", {}, id="b1"), raw_tool_json={"b1": '{"command": "ls'})])
    acc, _ = _run(bad)
    tu = acc.message().tool_uses()[0]
    assert tu.parse_error and tu.raw_input.startswith('{"command"')


def test_thinking_only_detection():
    m = ScriptedModel([turn(ThinkingBlock("hmm"))])
    acc, _ = _run(m)
    assert acc.message().is_thinking_only()
    assert acc.progress == "thinking_only"


def test_transcript_row_round_trip():
    a = Message.assistant([TextBlock("hi"), ToolUseBlock("t1", "Read", {"file_path": "/a"})], usage=Usage(10, 5), stop_reason="tool_use", message_id="msg1")
    r = Message.user([ToolResultBlock("t1", "content")])
    r.source_tool_assistant_uuid = a.uuid
    e = Message.assistant("oops", is_api_error=True, api_error=ApiErrorDetail(ErrorKind.PROMPT_TOO_LONG, "too long", 400, 210_000, 200_000))
    att = Attachment("date", {"date": "2026-01-01"})
    for item in (a, r, e, att):
        row = item_to_row(item, session_id="s", cwd="/p")
        back = row_to_item(row)
        assert back == item


def test_context_tokens_anchor_and_estimate():
    a = Message.assistant("x", usage=Usage(input_tokens=1000, output_tokens=50))
    u = Message.user("y" * 400)
    assert context_tokens([Message.user("hello"), a, u]) == 1050 + 100
    assert estimate_text_tokens("abcd") == 1
    boundary = Message.system("boundary", subtype="compact_boundary")
    assert context_tokens([a, boundary, u]) == 100


def test_loop_state_next_resets_guards_only_on_next_turn():
    s = LoopState(items=[])
    s.guards.output_limit_recoveries = 2
    s2 = s.next(TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY)
    assert s2.guards.output_limit_recoveries == 2 and s2.turn_count == 0
    s3 = s2.next(TransitionReason.NEXT_TURN, append=[Message.user("x")])
    assert s3.guards.output_limit_recoveries == 0 and s3.turn_count == 1 and len(s3.items) == 1


def test_utils():
    fm = parse_frontmatter("---\nname: x\ntools: [Read, Grep]\nmetadata:\n  type: user\nlist:\n  - a\n  - b\n---\nbody\n")
    assert fm.data == {"name": "x", "tools": ["Read", "Grep"], "metadata": {"type": "user"}, "list": ["a", "b"]}
    assert fm.body == "body"
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "required": ["n"], "additionalProperties": False}
    assert validate({"n": 2}, schema) == []
    assert len(validate({"n": 0, "x": 1}, schema)) == 2
    assert "<\\/system-reminder>" in wrap_system_reminder("a</system-reminder>b")
    assert head("a\nb\nc\n", max_lines=2) == ("a\nb\n", True)
    s = Settings.from_layers({SettingsSource.USER: {"permissions": {"allow": ["Read"]}}, SettingsSource.PROJECT: {"permissions": {"allow": ["Bash(ls)"]}, "x": 1}})
    assert s.get("permissions.allow") == ["Read", "Bash(ls)"] and s.source_of("x") == SettingsSource.PROJECT
    assert env_int({"N": "999"}, "N", 5, hi=15) == 15


def test_abort_children():
    parent = AbortController()
    child = parent.child()
    sibling = parent.child()
    child.abort("one")
    assert child.signal.aborted and not parent.signal.aborted and not sibling.signal.aborted
    parent.abort("all")
    assert sibling.signal.aborted and sibling.signal.reason == "all"


def test_tasks_and_notifications():
    reg, q = TaskRegistry(), NotificationQueue()
    reg.on_finish(lambda t: q.notify(t.id, t.status, t.description, str(t.result)))
    t = reg.register("shell", "sleep")
    reg.finish(t.id, "completed", result="ok")
    msgs = q.drain()
    assert len(msgs) == 1 and msgs[0].is_meta and f"<task-id>{t.id}</task-id>" in msgs[0].text()
    assert "</result>" in render_task_notification("x", "completed", "s", "r")
