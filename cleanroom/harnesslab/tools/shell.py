"""Snapshot-based shell execution: no persistent shell (WP2).

Implements spec 03 R38-R46 and 03 §4.5; takeaways 03#T9 (snapshot, fresh
``-c`` shell per command, cwd read back from a temp file), 03#T10 (background
on timeout instead of killing; block long leading sleeps); P14.

* ``ShellSnapshot.capture``: once per session, write a script that re-creates
  user functions (one ``eval`` per function so a bad one only drops itself),
  shopt state, aliases and PATH; optional binary shadowing hooks (03 R37) and a
  pkill guard against the harness pid.
* ``ShellRunner.run``: reject control characters (R44) and leading
  ``sleep N`` with N >= 25 s in the foreground (R42); compose
  ``source snapshot && ... && eval <cmd> && pwd -P >| cwdfile``; wrap with
  ``CLAUDE_CODE_SHELL_PREFIX`` (R46) and with the sandbox wrapper if configured
  (``pseudo.sandbox_profiles``, WP3); timeout default 120 000 ms, max 600 000
  (``BASH_DEFAULT_TIMEOUT_MS`` / ``BASH_MAX_TIMEOUT_MS``); on timeout the main
  agent's command moves to the background (``ctx.tasks`` +
  ``ctx.notifications``) and the result names the task id and output file
  (R40-R41); subagents kill.
* cwd persists across calls, shell variables do not; the new cwd is validated
  (no dot segments / device / network paths) and reset with a note if outside
  allowed dirs or ``ctx.pinned_cwd`` (R39).
* ``head_truncate``: default 30 000 chars, "... [N lines truncated] ..." (R26).

Design choices (spec silent)
----------------------------
* Commands always run under **bash** (``ctx.env["HARNESSLAB_SHELL"]`` or the
  first ``bash`` on PATH) so the snapshot syntax is predictable. Spawned
  processes get ``os.environ`` overlaid with ``ctx.env``.
* stdout and stderr are merged into one output *file* (not a pipe): a command
  that is moved to the background keeps writing to the same file, which then
  becomes the task's output file.
* Each command runs in its own process group so timeout-kill and interrupts
  reach its children.
* ``CLAUDE_CODE_SHELL_PREFIX`` is invoked as ``<prefix> '<script>'`` - the prefix
  program receives the full composed script as its last argument.
* Sandboxing: if ``ctx.extra["sandbox"]`` holds ``(backend, cfg)`` (WP3
  ``SandboxBackend``), the composed script is passed through ``backend.wrap``.
* ``CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS`` replaces the default wait when the
  model gave no explicit timeout.

Deferred
--------
* 03 R37 bundled bfs/ugrep/rg shadowing on native builds (``capture`` takes a
  ``shadows`` map as the hook), R45 harness-applied ``sed -i`` previews, the
  user keystroke that backgrounds a running command (R41, UI concern).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from harnesslab.config import env_int
from harnesslab.runtime import SessionContext
from harnesslab.util.abort import AbortSignal

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
BLOCKED_SLEEP_S = 25
BASH_MAX_OUTPUT_DEFAULT = 30_000
BASH_MAX_OUTPUT_LO, BASH_MAX_OUTPUT_HI = 4_000, 150_000
POLL_S = 0.02

#: C0 controls except \t and \n, DEL, C1 controls, and invisible/bidi
#: formatting characters that could hide text in an approval prompt (R44).
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f​-‏‪-‮⁦-⁩﻿]|\r(?!\n)"
)
_SLEEP_RE = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)([smhd]?)\s*(?:$|;|&&|\|\||\n)")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int | None
    interrupted: bool = False
    backgrounded_task_id: str | None = None
    output_file: Path | None = None
    cwd_note: str | None = None
    timed_out: bool = False


# ------------------------------------------------------------ pure helpers


def has_control_chars(cmd: str) -> bool:
    """R44: control/invisible characters could hide part of a command from the approver."""
    return bool(_CONTROL_RE.search(cmd))


def leading_sleep_seconds(cmd: str) -> float | None:
    """Seconds of a leading ``sleep N`` (alone or followed by ``;``/``&&``/``||``)."""
    m = _SLEEP_RE.match(cmd)
    if not m:
        return None
    return float(m.group(1)) * _UNIT[m.group(2)]


def head_truncate(text: str, max_chars: int = BASH_MAX_OUTPUT_DEFAULT) -> str:
    """R26: keep the head (whole lines) and say how many lines were dropped."""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    rest = text[len(head) :].lstrip("\n")
    dropped = rest.count("\n") + (0 if rest.endswith("\n") else 1)
    return f"{head}\n\n... [{dropped} lines truncated] ..."


def bash_max_output(env) -> int:
    return env_int(env, "BASH_MAX_OUTPUT_LENGTH", BASH_MAX_OUTPUT_DEFAULT, lo=BASH_MAX_OUTPUT_LO, hi=BASH_MAX_OUTPUT_HI)


def resolve_timeout_ms(requested: int | None, env) -> int:
    hi = env_int(env, "BASH_MAX_TIMEOUT_MS", MAX_TIMEOUT_MS, lo=1)
    if requested is None:
        default = env_int(env, "BASH_DEFAULT_TIMEOUT_MS", DEFAULT_TIMEOUT_MS, lo=1)
        default = env_int(env, "CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS", default, lo=1)
        return min(default, hi)
    return max(1, min(int(requested), hi))


def find_shell(env) -> str:
    return env.get("HARNESSLAB_SHELL") or shutil.which("bash") or "/bin/bash"


def valid_cwd_path(p: str) -> bool:
    """R39: absolute, no dot segments, not a device or network path."""
    if not p or not os.path.isabs(p):
        return False
    if p.startswith(("//", "\\\\")) or p.startswith("/dev/") or p == "/dev":
        return False
    return not any(seg in (".", "..") for seg in p.split("/")[1:])


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def allowed_roots(ctx: SessionContext) -> list[Path]:
    roots = [ctx.original_cwd or ctx.cwd, *ctx.additional_dirs]
    roots += [Path(d) for d in ctx.permissions.additional_directories]
    out: list[Path] = []
    for r in roots:
        out.append(Path(r))
        try:
            out.append(Path(os.path.realpath(r)))
        except OSError:
            pass
    return out


# --------------------------------------------------------------- snapshot

_CAPTURE_SCRIPT = r"""
for __f in $(declare -F | awk '{print $3}'); do
  case "$__f" in _*) continue;; esac
  printf 'eval %q 2>/dev/null\n' "$(declare -f "$__f")"
done
shopt -p 2>/dev/null | grep -v extglob
printf 'shopt -s expand_aliases\n'
alias -p 2>/dev/null
printf 'export PATH=%q\n' "$PATH"
"""

_PKILL_GUARD = r"""
__harness_guard() {
  case " $* " in
    *"%(pid)s"*|*harnesslab*) echo "refusing to signal the harness process" >&2; return 1;;
  esac
  return 0
}
pkill() { __harness_guard "$@" || return 1; command pkill "$@"; }
killall() { __harness_guard "$@" || return 1; command killall "$@"; }
"""


class ShellSnapshot:
    """The once-per-session environment script sourced by every command (R38)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def capture(self, user_shell: str = "/bin/bash", *, env: dict[str, str] | None = None, shadows: dict[str, str] | None = None, timeout_s: float = 10.0) -> bool:
        """Write the snapshot script. ``shadows`` maps a command name to a
        replacement command line (03 R37 binary shadowing hook). Returns False
        (and leaves no file) if the login shell could not be interrogated."""
        try:
            proc = subprocess.run(
                [user_shell, "-l", "-c", _CAPTURE_SCRIPT],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        lines = ["# harnesslab shell snapshot (generated; safe to delete)", proc.stdout.rstrip("\n")]
        for name, replacement in (shadows or {}).items():
            lines.append(f"{name}() {{ {replacement} \"$@\"; }}")
        lines.append(_PKILL_GUARD % {"pid": os.getpid()})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            return False
        return True


# ------------------------------------------------------------------ runner


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _ensure_tasks(ctx: SessionContext):
    from harnesslab.agents.notifications import NotificationQueue
    from harnesslab.agents.tasks import TaskRegistry

    if ctx.tasks is None:
        ctx.tasks = TaskRegistry()
    if ctx.notifications is None:
        ctx.notifications = NotificationQueue()
    return ctx.tasks, ctx.notifications


class ShellRunner:
    def __init__(self, ctx: SessionContext, snapshot: ShellSnapshot | None = None, *, login_if_no_snapshot: bool = True) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.login_if_no_snapshot = login_if_no_snapshot

    # -- composition -------------------------------------------------------
    def compose(self, command: str, cwd_file: Path, *, sandbox: bool = True) -> tuple[list[str], dict[str, str]]:
        """03 §4.5: ``source snapshot && [source hook env files] && shopt -u extglob
        && eval cmd && pwd -P >| file`` (hook env files: 04 R38)."""
        shell = find_shell(self.ctx.env)
        parts: list[str] = []
        login = True
        if self.snapshot is not None and self.snapshot.exists():
            parts.append(f"source {shlex.quote(str(self.snapshot.path))}")
            login = False
        # 04 R38: env files written by SessionStart/CwdChanged/... hooks (CLAUDE_ENV_FILE);
        # sourced only if they exist, failures ignored so a bad file never blocks a command.
        for env_file in self.ctx.extra.get("hook_env_files") or []:
            q = shlex.quote(str(env_file))
            parts.append(f"{{ [ -f {q} ] && . {q} >/dev/null 2>&1 || true; }}")
        parts.append("{ shopt -u extglob 2>/dev/null || true; }")
        parts.append(f"eval {shlex.quote(command)}")
        parts.append(f"pwd -P >| {shlex.quote(str(cwd_file))}")
        script = " && ".join(parts)
        prefix = self.ctx.env.get("CLAUDE_CODE_SHELL_PREFIX")
        extra_env: dict[str, str] = {}
        sb = self.ctx.extra.get("sandbox") if sandbox else None
        if sb is not None:
            backend, cfg = sb
            argv, extra_env = backend.wrap(script, cfg)
            return argv, extra_env
        if prefix:
            inner = [shell, "-c", script]
            script = f"{prefix} {shlex.join(inner)}"
        argv = [shell] + (["-l"] if (login and self.login_if_no_snapshot) else []) + ["-c", script]
        return argv, extra_env

    # -- execution ---------------------------------------------------------
    def run(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        background: bool = False,
        abort: AbortSignal | None = None,
        sandbox: bool = True,
    ) -> ShellResult:
        """Run ``command`` in a fresh shell. ``abort`` is the per-call signal
        (01 R12); ``sandbox=False`` honours ``dangerouslyDisableSandbox``."""
        ctx = self.ctx
        if has_control_chars(command):
            raise ValueError("command contains control characters")
        secs = leading_sleep_seconds(command)
        if not background and secs is not None and secs >= BLOCKED_SLEEP_S:
            raise ValueError(f"leading sleep of {secs:g}s is blocked in the foreground")

        tmpdir = Path(tempfile.gettempdir())
        token = uuid.uuid4().hex[:12]
        cwd_file = tmpdir / f"harnesslab-{token}-cwd"
        if background:
            ctx.tasks_dir.mkdir(parents=True, exist_ok=True)
            out_path = ctx.tasks_dir / f"shell-{token}.output"
        else:
            out_path = tmpdir / f"harnesslab-{token}.out"
        argv, extra_env = self.compose(command, cwd_file, sandbox=sandbox)
        env = {**os.environ, **dict(ctx.env), **extra_env}
        out_f = open(out_path, "wb")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(ctx.cwd),
                stdout=out_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        finally:
            out_f.close()

        if background:
            task_id = self._adopt_as_task(proc, command, out_path, cwd_file)
            return ShellResult("", "", None, backgrounded_task_id=task_id, output_file=out_path)

        deadline = time.monotonic() + resolve_timeout_ms(timeout_ms, ctx.env) / 1000.0
        abort = abort or ctx.abort.signal
        interrupted = timed_out = False
        while proc.poll() is None:
            if abort.aborted:
                interrupted = True
                _kill_group(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            abort.wait(POLL_S)

        if timed_out and ctx.is_main_agent:
            # R40: keep it running; the output file becomes the task's output.
            bg_dir = ctx.tasks_dir
            bg_dir.mkdir(parents=True, exist_ok=True)
            task_path = bg_dir / out_path.name
            try:
                os.replace(out_path, task_path)  # same inode, writer unaffected on POSIX
            except OSError:
                task_path = out_path
            task_id = self._adopt_as_task(proc, command, task_path, cwd_file)
            partial = _read_capped(task_path)
            return ShellResult(partial, "", None, backgrounded_task_id=task_id, output_file=task_path, timed_out=True)
        if timed_out:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
        output = _read_capped(out_path)
        _unlink(out_path)
        note = self._update_cwd(cwd_file)
        return ShellResult(
            output,
            "",
            None if (timed_out or interrupted) else proc.returncode,
            interrupted=interrupted,
            cwd_note=note,
            timed_out=timed_out,
        )

    def _adopt_as_task(self, proc: subprocess.Popen, command: str, out_path: Path, cwd_file: Path) -> str:
        """Register ``proc`` as a background shell task and notify on exit (R41)."""
        tasks, notes = _ensure_tasks(self.ctx)
        task = tasks.register("shell", command[:200], output_file=out_path, stop=lambda: _kill_group(proc))
        task.meta["command"] = command

        def waiter() -> None:
            code = proc.wait()
            _unlink(cwd_file)  # background commands never change the session cwd
            current = tasks.get(task.id)
            if current is None or current.status != "running":
                return
            status = "completed" if code == 0 else "failed"
            task.meta["notified"] = True  # notified below; generic on_finish listeners skip it
            tasks.finish(task.id, status, result={"exit_code": code})
            notes.notify(
                task.id,
                status,
                f'Background command "{command[:120]}" finished with exit code {code}.',
                result=f"Output file: {out_path}",
            )

        threading.Thread(target=waiter, name=f"shell-task-{task.id}", daemon=True).start()
        return task.id

    def _update_cwd(self, cwd_file: Path) -> str | None:
        """R39: cwd persists; validate it and reset when it escapes."""
        ctx = self.ctx
        try:
            new = cwd_file.read_text().strip()
        except OSError:
            return None
        finally:
            _unlink(cwd_file)
        if not new:
            return None
        current = str(ctx.cwd)
        if new in (current, os.path.realpath(current)):
            return None
        if ctx.pinned_cwd:
            return f"Note: the working directory is fixed for this agent; it stays {current}."
        p = Path(new)
        if valid_cwd_path(new) and any(_within(p, r) for r in allowed_roots(ctx)):
            ctx.cwd = p
            return None
        ctx.cwd = Path(ctx.original_cwd or ctx.cwd)
        return (
            f"Note: the command left the working directory at {new}, which is outside the allowed "
            f"directories, so it was reset to {ctx.cwd}."
        )


def _read_capped(path: Path, cap: int = 64 * 1024 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(cap)
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
