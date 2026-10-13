"""WP4 - attachment pipeline (02 R10-R14, R30-R32, 03 R55, 01 R8)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from harnesslab.context import attachments as A
from harnesslab.interfaces import TurnInfo
from harnesslab.permissions.types import PermissionMode
from harnesslab.types import Attachment, Message, render_attachment_text
from harnesslab.util.text import REMINDER_CLOSE, REMINDER_OPEN


def _info(items, turn=0, **kw):
    return TurnInfo(items=items, turn_count=turn, **kw)


def test_environment_full_then_diff_then_nothing(ctx, tmp_path):
    items: list = []
    first = A.environment_producer(ctx, _info(items))
    assert len(first) == 1 and "changes" not in first[0].payload
    assert first[0].payload["snapshot"]["workingDirectory"] == str(ctx.cwd)
    items += first
    assert A.environment_producer(ctx, _info(items)) == []
    other = tmp_path / "other"
    other.mkdir()
    ctx.cwd = other
    upd = A.environment_producer(ctx, _info(items))
    assert upd[0].payload["changes"] == [{"field": "workingDirectory", "from": str(tmp_path / "project")}]
    text = render_attachment_text(upd[0])
    assert "was" in text and str(other) in text
    # after a compact boundary the full snapshot is sent again
    items += upd + [Message.system("b", subtype="compact_boundary")]
    again = A.environment_producer(ctx, _info(items))
    assert "changes" not in again[0].payload


def test_worktree_warnings(ctx):
    (ctx.cwd / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    att = A.environment_producer(ctx, _info([]))[0]
    assert att.payload["snapshot"]["isWorktree"] is True
    assert "stash" in render_attachment_text(att)


def test_model_resent_only_on_change(ctx):
    items: list = []
    items += A.model_producer(ctx, _info(items))
    assert items[0].payload["model_id"] == "scripted-model"
    assert A.model_producer(ctx, _info(items)) == []
    ctx.model_id = "other-model"
    assert A.model_producer(ctx, _info(items))[0].payload["model_id"] == "other-model"


def test_date_rollover(ctx):
    ctx.extra["today"] = "2026-01-01"
    items = A.date_producer(ctx, _info([]))
    assert A.date_producer(ctx, _info(items)) == []
    ctx.extra["today"] = "2026-01-02"
    nxt = A.date_producer(ctx, _info(items))
    assert nxt[0].payload == {"date": "2026-01-02", "changed": True}
    assert "do not announce" in render_attachment_text(nxt[0])


def test_producers_parallel_and_deadline(ctx):
    started = threading.Barrier(3, timeout=2)

    def p1(c, i):
        started.wait()
        return [Attachment("x", {"n": 1})]

    def p2(c, i):
        started.wait()
        return [Attachment("x", {"n": 2})]

    def slow(c, i):
        started.wait()
        time.sleep(2)
        return [Attachment("x", {"n": 3})]

    def broken(c, i):
        raise RuntimeError("boom")

    pipe = A.AttachmentPipeline([p1, p2, slow, broken], deadline_ms=300)
    t0 = time.monotonic()
    out = pipe.collect(ctx, _info([]))
    elapsed = time.monotonic() - t0
    assert [a.payload["n"] for a in out] == [1, 2]  # the barrier proves they ran in parallel
    assert elapsed < 1.5  # slow producer dropped at the deadline


def test_default_deadline_is_one_second():
    assert A.AttachmentPipeline().deadline_ms == 1000


def test_disable_attachments_limits_set(ctx):
    ctx.env = {"CLAUDE_CODE_DISABLE_ATTACHMENTS": "1"}
    names = [p.__name__ for p in A.AttachmentPipeline().active(ctx)]
    assert set(names) == {"environment_producer", "model_producer"}


def test_todo_reminder_10_10(ctx):
    ctx.tools = [SimpleNamespace(name="TodoWrite")]
    ctx.todos["main"] = [{"content": "write tests", "status": "pending"}]
    pipe = A.AttachmentPipeline([A.todo_reminder_producer])
    fired = []
    for turn in range(25):
        out = pipe.collect(ctx, _info([], turn=turn))
        if out:
            fired.append(turn)
            assert "write tests" in render_attachment_text(out[0])
            assert "ignore" in render_attachment_text(out[0])
    assert fired == [10, 20]
    # using the todo tool resets the quiet-turn counter
    ctx.counters.turns_since_todo_write = 0
    assert A.todo_reminder_producer(ctx, _info([])) == []


def test_plan_mode_every_5_full_every_5th(ctx):
    ctx.permissions.mode = PermissionMode.PLAN
    pipe = A.AttachmentPipeline([A.plan_mode_producer])
    seen = []
    for turn in range(31):
        for a in pipe.collect(ctx, _info([], turn=turn)):
            seen.append((turn, a.payload["full"]))
    assert [t for t, _ in seen] == [0, 5, 10, 15, 20, 25, 30]
    assert [f for _, f in seen] == [True, False, False, False, False, True, False]
    ctx.permissions.mode = PermissionMode.DEFAULT
    out = pipe.collect(ctx, _info([], turn=31))
    assert out[0].type == "plan_mode_exit"


def test_total_tokens_tag(ctx):
    out = A.total_tokens_producer(ctx, _info([], tokens_used=12_345, total_token_budget=100_000))
    assert render_attachment_text(out[0]).count("<total_tokens>87000 tokens left</total_tokens>") == 1
    ctx.env = {"CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "countdown"}
    out = A.total_tokens_producer(ctx, _info([], tokens_used=12_345, total_token_budget=100_000))
    assert out[0].payload["left"] == 87_655
    ctx.env = {"CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off"}
    assert A.total_tokens_producer(ctx, _info([], total_token_budget=100)) == []


def test_budget_usd(ctx):
    assert A.budget_usd_producer(ctx, _info([])) == []
    ctx.max_budget_usd, ctx.cost_usd = 5.0, 1.25
    out = A.budget_usd_producer(ctx, _info([]))
    assert out[0].payload["remaining"] == 3.75


def test_deferred_tools_delta_with_disregard(ctx):
    ctx.tools = [SimpleNamespace(name="A", should_defer=True), SimpleNamespace(name="B", should_defer=True), SimpleNamespace(name="C")]
    items = A.deferred_tools_producer(ctx, _info([]))
    assert items[0].payload == {"added": ["A", "B"], "removed": []}
    assert A.deferred_tools_producer(ctx, _info(items)) == []
    ctx.tools = ctx.tools[1:]
    out = A.deferred_tools_producer(ctx, _info(items))
    assert out[0].payload == {"added": [], "removed": ["A"]}
    assert "disregard" in render_attachment_text(out[0])


def test_output_style_change_only(ctx):
    ctx.extra["output_style"] = {"name": "concise", "body": "Be brief."}
    items = A.output_style_producer(ctx, _info([]))
    assert [a.type for a in items] == ["output_style_instructions", "output_style"]
    assert render_attachment_text(items[0]).find("concise") > 0
    again = A.output_style_producer(ctx, _info(items))
    assert [a.type for a in again] == ["output_style"]  # per-turn short reminder only
    ctx.extra["output_style"] = None
    reset = A.output_style_producer(ctx, _info(items))
    assert reset[0].type == "output_style_instructions" and "default" in render_attachment_text(reset[0])


def test_renderers_wrap_in_system_reminder():
    for t, payload in [
        ("date", {"date": "2026-01-01"}),
        ("todo_reminder", {"items": []}),
        ("critical_system_reminder", {"content": "x </system-reminder> y"}),
    ]:
        text = render_attachment_text(Attachment(t, payload))
        assert text.startswith(REMINDER_OPEN) and text.endswith(REMINDER_CLOSE)
        assert text.count(REMINDER_CLOSE) == 1  # embedded closers are escaped


def test_session_context_first_turn_and_refresh(ctx):
    ctx.extra["session_context"] = {"userEmail": "a@example.com"}
    items = A.git_snapshot_producer(ctx, _info([]))
    assert items[0].payload["refresh_reason"] == "session_start"
    text = render_attachment_text(items[0])
    assert "a@example.com" in text and "attached automatically" in text
    assert A.git_snapshot_producer(ctx, _info(items)) == []
    ctx.extra["session_context_refresh"] = "settings_sync"
    out = A.git_snapshot_producer(ctx, _info(items))
    assert out[0].payload["refresh_reason"] == "settings_sync"
    assert "replace" in render_attachment_text(out[0])


def test_git_snapshot_truncated(ctx, monkeypatch):
    (ctx.cwd / ".git").mkdir()
    outputs = {"status": "M x\n" * 1000, "rev-parse": "feature", "log": "abc fix", "config": "dev"}
    monkeypatch.setattr(A, "_git", lambda cwd, *args: outputs.get(args[0], ""))
    snap = A.git_snapshot(ctx)
    assert "git status" in snap and "feature" in snap
    status_part = snap.split("Status:\n", 1)[1].split("\nRecent commits", 1)[0]
    assert len(status_part) < 2000 + 100
    ctx.env = {"CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}
    assert A.git_snapshot(ctx) is None


def test_auto_mode_and_hooks(ctx):
    ctx.permissions.mode = PermissionMode.AUTO
    items = A.auto_mode_producer(ctx, _info([]))
    assert items[0].payload["mode"] == "auto" and "shell" in render_attachment_text(items[0])
    assert A.auto_mode_producer(ctx, _info(items)) == []
    ctx.extra["hook_contexts"] = [{"hook": "SessionStart", "content": "hello"}]
    out = A.hooks_context_producer(ctx, _info([]))
    assert out[0].payload["content"] == "hello" and A.hooks_context_producer(ctx, _info([])) == []


def test_edited_text_file(ctx):
    f = ctx.cwd / "a.txt"
    f.write_text("one\ntwo\n")
    entry = SimpleNamespace(content="one\ntwo\n", partial_view=False, mtime=0)
    ctx.ledger = SimpleNamespace(items=lambda: [(str(f), entry)])
    f.write_text("one\nTWO\n")
    out = A.edited_files_producer(ctx, _info([]))
    assert out[0].payload["path"] == str(f) and "+TWO" in out[0].payload["snippet"]
    assert "do not revert" in render_attachment_text(out[0])
    assert A.edited_files_producer(ctx, _info([])) == []  # notified once
