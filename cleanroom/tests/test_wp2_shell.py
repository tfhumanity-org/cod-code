"""WP2 shell runner and Bash tool: 03 R26, R38-R46, §4.5 (03#T9, 03#T10). POSIX only."""

from __future__ import annotations

import os
import time

import pytest

from harnesslab.tools.base import ToolErrorCode
from harnesslab.tools.builtin.bash import BashTool
from harnesslab.tools.pipeline import run_tool_use
from harnesslab.tools.shell import (
    ShellRunner,
    ShellSnapshot,
    has_control_chars,
    head_truncate,
    leading_sleep_seconds,
    resolve_timeout_ms,
)

from tests.wp2_fakes import allow_all, block, call_ctx_for, wait_until

pytestmark = pytest.mark.skipif(os.name != "posix", reason="shell runner tests need a POSIX shell")


@pytest.fixture
def sctx(ctx):
    ctx.can_use_tool = allow_all
    ctx.extra["shell_snapshot"] = None  # skip login-shell capture in Bash tool tests
    return ctx


def runner(ctx):
    return ShellRunner(ctx, None, login_if_no_snapshot=False)


# ------------------------------------------------------------------ helpers


def test_control_characters_detected():
    assert not has_control_chars("echo hi\n\tls")
    assert has_control_chars("echo \x1b[31m")
    assert has_control_chars("rm -rf /tmp/x\rls")  # carriage return can hide text
    assert has_control_chars("echo ‮evil")  # bidi override
    assert not has_control_chars("printf 'a'\r\n")


def test_leading_sleep_seconds():
    assert leading_sleep_seconds("sleep 30") == 30
    assert leading_sleep_seconds("  sleep 2m && make") == 120
    assert leading_sleep_seconds("sleep 1.5; ls") == 1.5
    assert leading_sleep_seconds("make && sleep 30") is None
    assert leading_sleep_seconds("sleepy 30") is None


def test_head_truncate():
    text = "\n".join(f"line {i}" for i in range(100))
    out = head_truncate(text, 50)
    assert out.startswith("line 0\nline 1") and len(out.split("\n\n...")[0]) <= 50
    kept = out.split("\n\n...")[0].count("\n") + 1
    assert f"[{100 - kept} lines truncated]" in out
    assert head_truncate("short", 50) == "short"


def test_timeout_resolution():
    assert resolve_timeout_ms(None, {}) == 120_000
    assert resolve_timeout_ms(10**9, {}) == 600_000
    assert resolve_timeout_ms(None, {"BASH_DEFAULT_TIMEOUT_MS": "5000"}) == 5000
    assert resolve_timeout_ms(700_000, {"BASH_MAX_TIMEOUT_MS": "700000"}) == 700_000


# ------------------------------------------------------------------- runner


def test_cwd_persists_but_variables_do_not(sctx, project):
    (project / "sub").mkdir()
    r = runner(sctx)
    res = r.run("cd sub && export FOO=bar && echo $FOO")
    assert res.stdout.strip() == "bar" and res.exit_code == 0
    assert os.path.realpath(sctx.cwd) == os.path.realpath(project / "sub")
    res2 = r.run('pwd -P; echo "${FOO:-unset}"')
    assert res2.stdout.split() == [os.path.realpath(project / "sub"), "unset"]


def test_cwd_outside_allowed_dirs_is_reset(sctx, project):
    res = runner(sctx).run("cd /")
    assert sctx.cwd == project
    assert "outside the allowed directories" in res.cwd_note


def test_pinned_cwd_is_not_changed(sctx, project):
    (project / "sub").mkdir()
    sctx.pinned_cwd = True
    res = runner(sctx).run("cd sub")
    assert sctx.cwd == project and "fixed for this agent" in res.cwd_note


def test_runner_rejects_control_chars_and_long_sleep(sctx):
    r = runner(sctx)
    with pytest.raises(ValueError):
        r.run("echo \x07")
    with pytest.raises(ValueError):
        r.run("sleep 30")


def test_exit_code_and_stderr_are_captured(sctx):
    res = runner(sctx).run("echo out; echo err >&2; exit 3")
    assert res.exit_code == 3 and "out" in res.stdout and "err" in res.stdout


def test_timeout_moves_main_agent_command_to_background(sctx):
    res = runner(sctx).run("echo started; sleep 0.6; echo finished", timeout_ms=150)
    assert res.timed_out and res.backgrounded_task_id
    assert "started" in res.stdout
    task = sctx.tasks.get(res.backgrounded_task_id)
    assert task.kind == "shell" and task.output_file == res.output_file
    assert wait_until(lambda: len(sctx.notifications) > 0, timeout=5)
    note = sctx.notifications.drain()[0].text()
    assert res.backgrounded_task_id in note and "completed" in note
    assert "finished" in res.output_file.read_text()
    assert sctx.tasks.get(res.backgrounded_task_id).status == "completed"


def test_subagent_command_is_killed_on_timeout(sctx):
    sctx.agent_id = "a1"
    t0 = time.monotonic()
    res = runner(sctx).run("sleep 5", timeout_ms=100)
    assert res.timed_out and res.backgrounded_task_id is None and res.exit_code is None
    assert time.monotonic() - t0 < 4


def test_run_in_background_registers_task(sctx):
    res = runner(sctx).run("echo bg-out", background=True)
    assert res.backgrounded_task_id and res.output_file.parent == sctx.tasks_dir
    assert wait_until(lambda: sctx.tasks.get(res.backgrounded_task_id).status != "running", timeout=5)
    assert "bg-out" in res.output_file.read_text()


def test_abort_interrupts_command(sctx):
    from harnesslab.util.abort import AbortController
    import threading

    c = AbortController()
    threading.Timer(0.1, c.abort).start()
    t0 = time.monotonic()
    res = runner(sctx).run("sleep 5", abort=c.signal)
    assert res.interrupted and time.monotonic() - t0 < 4


def test_snapshot_is_sourced(sctx, tmp_path):
    snap = ShellSnapshot(tmp_path / "snap.sh")
    snap.path.write_text("greet() { echo hello-from-snapshot; }\n")
    res = ShellRunner(sctx, snap).run("greet")
    assert res.stdout.strip() == "hello-from-snapshot"


def test_snapshot_capture_writes_script(tmp_path):
    snap = ShellSnapshot(tmp_path / "s" / "snap.sh")
    assert snap.capture("bash", env={**os.environ, "HOME": str(tmp_path)})
    text = snap.path.read_text()
    assert "export PATH=" in text and "pkill()" in text
    assert oct(snap.path.stat().st_mode & 0o777) == "0o600"


def test_shell_prefix_wraps_command(sctx, tmp_path):
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text('#!/bin/sh\necho PREFIXED\nexec "$@"\n')
    wrapper.chmod(0o755)
    sctx.env = {"CLAUDE_CODE_SHELL_PREFIX": str(wrapper)}
    res = runner(sctx).run("echo inner")
    assert res.stdout.split() == ["PREFIXED", "inner"]


# ---------------------------------------------------------------- Bash tool


def run_bash(ctx, **inp):
    o = run_tool_use(block("Bash", "t1", **inp), BashTool(), call_ctx_for(ctx))
    return o, o.result.tool_results()[0].text()


def test_bash_tool_rejections_have_codes(sctx):
    o, t = run_bash(sctx, command="sleep 40")
    assert o.error_code == ToolErrorCode.BLOCKED_SLEEP and "run_in_background" in t
    o, t = run_bash(sctx, command="echo \x1b")
    assert o.error_code == ToolErrorCode.CONTROL_CHARACTERS


def test_bash_tool_output_exit_code_and_truncation(sctx):
    o, t = run_bash(sctx, command="echo hi; exit 2")
    assert t.startswith("hi") and "Exit code 2" in t
    sctx.env = {"BASH_MAX_OUTPUT_LENGTH": "4000"}
    o, t = run_bash(sctx, command="seq 1 5000")
    assert "lines truncated" in t and len(t) < 4200


def test_bash_tool_empty_output_note(sctx):
    o, t = run_bash(sctx, command="true")
    assert t == "(Bash completed with no output)"


def test_bash_tool_background_message(sctx):
    o, t = run_bash(sctx, command="echo x", run_in_background=True)
    assert "task id" in t and "notification" in t


def test_bash_tool_formatter_note(sctx, project):
    from harnesslab.tools.ledger import ReadLedger

    sctx.ledger = ReadLedger()
    f = project / "a.py"
    f.write_text("x=1\n")
    sctx.ledger.record_read(str(f), f.read_text(), offset=None, limit=None, partial=False)
    time.sleep(0.01)
    o, t = run_bash(sctx, command=f"touch {f} # --fix")
    assert "modified 1 file(s)" in t


def test_bash_data_uri_becomes_image(sctx):
    o, _ = run_bash(sctx, command="echo 'data:image/png;base64,iVBORw0KGgo='")
    content = o.result.tool_results()[0].content
    assert content[0].type == "image" and content[0].media_type == "image/png"


def test_bash_read_only_uses_wp3_analysis(monkeypatch):
    tool = BashTool()
    from harnesslab.permissions import bash_safety

    monkeypatch.setattr(bash_safety, "is_read_only", lambda cmd: (_ for _ in ()).throw(NotImplementedError()))
    assert tool.is_concurrency_safe({"command": "ls"}) is False  # fail safe
    monkeypatch.setattr(bash_safety, "is_read_only", lambda cmd: cmd.startswith("ls"))
    assert tool.is_concurrency_safe({"command": "ls -la"}) is True
    assert tool.is_concurrency_safe({"command": "rm x"}) is False
    assert tool.classifier_projection({"command": "rm x"}) == "rm x"
