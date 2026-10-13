"""Bash tool (WP2).

Implements spec 03 §2.2 (Bash row: 30k head truncation, 128k spill ceiling,
``backgrounding = "self"``), R38-R47 via ``tools.shell.ShellRunner``, R43
(``is_read_only`` = ``permissions.bash_safety.is_read_only(command)`` and
``is_concurrency_safe = is_read_only``; import it lazily - WP3), R22 (after a
formatter-like command, ``ctx.ledger.after_shell`` note), R47 (``description``
is for the human approver), 02 R38 (git commit / PR procedure lives in this
tool's prompt, not the system prompt), 04 R45 (``dangerouslyDisableSandbox``
input). ``check_permissions`` delegates to ``permissions.bash_safety.check_bash``
(lazy import). ``classifier_projection`` = the command.

Design choices (spec silent)
----------------------------
* The session's shell snapshot is captured lazily on the first Bash call and
  kept in ``ctx.extra["shell_snapshot"]`` (``None`` when capture failed or
  ``HARNESSLAB_DISABLE_SHELL_SNAPSHOT`` is set). Tests can pre-seed the key.
* If WP3's analysis is unavailable (raises), the command is treated as *not*
  read-only (fail safe: serial) and permissions fall through to the pipeline.
* A data-URI image on stdout (R46) becomes an image block.
* Deferred: in-place ``sed`` preview/apply with base-hash check (R45), gh
  rate-limit hints and working-tree diffs (R46 "MAY" parts), binary shadowing
  on native builds (R37; the snapshot has a ``shadows`` hook for it).
"""

from __future__ import annotations

import os
import re
from typing import Any

from harnesslab.config import env_truthy
from harnesslab.permissions.types import Decision
from harnesslab.tools.base import Tool, ToolCallContext, ToolErrorCode, ToolOutput, ValidationResult
from harnesslab.tools.shell import (
    BLOCKED_SLEEP_S,
    ShellRunner,
    ShellSnapshot,
    bash_max_output,
    find_shell,
    has_control_chars,
    head_truncate,
    leading_sleep_seconds,
)
from harnesslab.types import ImageBlock

_DATA_URI = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$")

_GIT_GUIDANCE = """
Committing with git (only when the user asks you to commit):
1. Run `git status`, `git diff` (staged and unstaged) and `git log -n 5 --oneline` in parallel to see
   what changed and how this repository words its commit messages.
2. Draft a short message that explains why the change was made. Do not commit files that look like
   secrets (.env, credentials).
3. Stage the relevant files by name, commit (pass the message via a heredoc), then run `git status`
   to confirm. If a pre-commit hook changes files, stage them and create a NEW commit; never amend
   unless asked.
Never change git config, never force-push to main, never skip hooks (--no-verify) unless the user
explicitly asks. Do not push unless asked.

Pull requests: inspect the full branch diff against the base branch first, push the branch with
`-u` if needed, then create the PR with `gh pr create` and a body that summarises the change and how
it was tested. Return the PR URL when done.
""".strip()


class BashTool(Tool):
    name = "Bash"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 600_000},
            "description": {"type": "string"},
            "run_in_background": {"type": "boolean"},
            "dangerouslyDisableSandbox": {"type": "boolean"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    max_result_chars = 30_000
    persistence_ceiling = 128_000
    backgrounding = "self"
    rule_content_field = "command"
    kind = "execute"
    search_hint = "run shell commands"

    def description(self, ctx: Any = None) -> str:
        return "Run a shell command."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        core = (
            "Runs a bash command in a fresh shell. The working directory persists between calls; shell "
            "variables and exported environment do not. Quote paths that contain spaces. Default timeout "
            "120000 ms (max 600000); a command that times out keeps running in the background and you are "
            "told its task id. For long-running work set run_in_background; a notification arrives once the "
            "command exits - do not poll with sleep. Output over 30000 characters is truncated.\n"
            "Prefer the dedicated tools: Read instead of cat/head/tail, Edit/Write instead of sed/echo "
            "redirection, Grep instead of grep/rg, Glob instead of find. Run independent commands as "
            "parallel calls; chain dependent ones with &&.\n"
            "`description`: a few words saying what the command does, for the person approving it. Describe "
            "the intent in plain words; do not repeat the command, its flags or paths."
        )
        if lean:
            return core + "\n\n" + _GIT_GUIDANCE
        return core + (
            "\n\nExamples:\n- {\"command\": \"pytest -q tests/test_api.py\", \"description\": \"Run the API tests\"}\n"
            "- {\"command\": \"npm run build\", \"run_in_background\": true, \"description\": \"Build the app\"}\n\n"
        ) + _GIT_GUIDANCE

    # ---------------------------------------------------------- predicates
    def is_read_only(self, input: dict[str, Any]) -> bool:
        """R43: provably read-only per WP3's analysis; anything else is not."""
        try:
            from harnesslab.permissions import bash_safety

            return bool(bash_safety.is_read_only(str(input.get("command", ""))))
        except Exception:
            return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return self.is_read_only(input)

    def validate_input(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ValidationResult:
        cmd = input["command"]
        if not cmd.strip():
            return ValidationResult.fail("The command is empty.", ToolErrorCode.INPUT_VALIDATION)
        if has_control_chars(cmd):
            return ValidationResult.fail(
                "The command contains control or invisible formatting characters, which could hide part "
                "of it from the person approving it. Remove them and retry.",
                ToolErrorCode.CONTROL_CHARACTERS,
            )
        secs = leading_sleep_seconds(cmd)
        if secs is not None and secs >= BLOCKED_SLEEP_S and not input.get("run_in_background"):
            return ValidationResult.fail(
                f"Blocked: the command starts with `sleep {secs:g}`. Long sleeps waste time and context. "
                "If you are waiting for something, run the work with run_in_background (a notification "
                "follows on exit) or use a monitoring tool. Do not split the wait into several "
                "shorter sleeps.",
                ToolErrorCode.BLOCKED_SLEEP,
            )
        return ValidationResult()

    def check_permissions(self, input: dict[str, Any], call_ctx: ToolCallContext) -> Decision:
        """When a session has ``can_use_tool`` (the real permission pipeline), that
        pipeline runs the shell analysis itself and combines it with this check, so
        this returns passthrough (no opinion). Without one (bare embeddings, unit
        tests), the analysis runs here so a dangerous command is still caught."""
        ctx = call_ctx.session
        if ctx.can_use_tool is not None:
            return Decision.passthrough()
        from harnesslab.permissions.bash_safety import check_bash

        home = ctx.env.get("HOME") or os.path.expanduser("~")
        return check_bash(str(input["command"]), ctx.permissions, cwd=str(ctx.cwd), home=home)

    # ------------------------------------------------------------------ call
    def _runner(self, call_ctx: ToolCallContext) -> ShellRunner:
        ctx = call_ctx.session
        if "shell_snapshot" not in ctx.extra:
            snap: ShellSnapshot | None = None
            if not env_truthy(ctx.env, "HARNESSLAB_DISABLE_SHELL_SNAPSHOT"):
                candidate = ShellSnapshot(ctx.session_state_dir / "shell-snapshot.sh")
                env = {**os.environ, **dict(ctx.env)}
                if candidate.capture(find_shell(ctx.env), env=env):
                    snap = candidate
            ctx.extra["shell_snapshot"] = snap
        return ShellRunner(ctx, ctx.extra["shell_snapshot"])

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        cmd: str = input["command"]
        background = bool(input.get("run_in_background"))
        runner = self._runner(call_ctx)
        res = runner.run(
            cmd,
            timeout_ms=input.get("timeout"),
            background=background,
            abort=call_ctx.abort,
            sandbox=not input.get("dangerouslyDisableSandbox", False),
        )
        data = {"stdout": res.stdout, "exit_code": res.exit_code, "interrupted": res.interrupted,
                "background_task_id": res.backgrounded_task_id}
        if res.backgrounded_task_id and not res.timed_out:
            return ToolOutput(
                data=(
                    f"Command running in the background with task id {res.backgrounded_task_id}. Output is "
                    f"being written to {res.output_file}. A notification will follow on exit; Read that "
                    "file to check progress in the meantime."
                )
            )
        notes: list[str] = []
        out = head_truncate(res.stdout.rstrip("\n"), bash_max_output(ctx.env))
        if res.timed_out and res.backgrounded_task_id:
            notes.append(
                f"The timeout elapsed before the command exited. It keeps running as background task "
                f"{res.backgrounded_task_id}, with output still going to {res.output_file}; "
                "a notification will follow once it exits."
            )
        elif res.timed_out:
            notes.append("The command timed out and was terminated.")
        if res.interrupted:
            notes.append("The command was interrupted before it finished.")
        if res.exit_code not in (0, None):
            notes.append(f"Exit code {res.exit_code}")
        if res.cwd_note:
            notes.append(res.cwd_note)
        if ctx.ledger is not None:
            note = ctx.ledger.after_shell(cmd)
            if note:
                notes.append(note)
        m = _DATA_URI.match(out.strip())
        if m:
            return ToolOutput(data=data, blocks=[ImageBlock(m.group(1), re.sub(r"\s", "", m.group(2)))], notes=notes)
        return ToolOutput(data=out, notes=notes, is_error=bool(res.interrupted))

    def classifier_projection(self, input: dict[str, Any]) -> str:
        return str(input.get("command", ""))
