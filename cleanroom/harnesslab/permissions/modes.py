"""Permission modes: residual-ask resolution and mode transitions (WP3).

Implements spec 04 §2.1 table (what each mode does with a residual "ask"),
04 R1 (bypass as ``defaultMode`` from project/local settings ignored), R2
(policy disables bypass/auto), R3 (runtime switch to bypass needs
``--dangerously-skip-permissions``; distinct errors; cloud rejects), R4 (plan
mode records/restores the previous mode; ``useAutoModeDuringPlan``), R11 +
§4.6 (allow-rule stripping in auto mode: interpreters, launchers, network
CLIs with args, mutating kubectl, ``autoMode.classifyAllShell``), §4.4
``resolve_ask`` / ``auto_mode`` (fast paths before the classifier; mandatory
asks never classified). Takeaway 04#T8.

Design choices where the spec is silent
---------------------------------------
* ``bubble`` (subagents, 06 R20) resolves like ``default``: the residual ask is
  shown to whoever the session's prompter forwards to.
* A runtime switch to bypass is "restricted" when ``permissions.restricted`` is
  truthy in settings (no restricted-mode concept exists otherwise here).
* ``first_read_outside_workdirs`` is applied to *every* read outside the
  working directories (stricter than "first").
* In acceptEdits, shell commands are auto-allowed only if every subcommand's
  base command is in the list *and* every path argument stays inside the working
  directories (the spec names only the base commands).
* In plan mode with ``useAutoModeDuringPlan`` (default true) and auto mode as
  the pre-plan mode, read-only residual asks get auto semantics.
* ``mode_change_message`` returns the distinct R3 error texts.
"""

from __future__ import annotations

import os
from typing import Any

from harnesslab.config import Settings, SettingsSource
from harnesslab.permissions.bash_safety import parse_shell, sed_has_write_or_exec
from harnesslab.permissions.classifier import DenialTracker, TwoStageClassifier
from harnesslab.permissions.hazards import (
    ACCEPT_EDITS_SHELL_COMMANDS,
    breaker,
    is_dangerous_removal_target,
    is_sensitive_path,
)
from harnesslab.permissions.types import (
    ClassifierReason,
    ClassifierVerdict,
    Decision,
    ModeReason,
    PermissionContext,
    PermissionMode,
    Rule,
    SafetyCheckReason,
)
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool

INTERPRETERS = ("python", "python3", "python2", "node", "deno", "tsx", "ruby", "perl", "php", "lua", "npx", "bunx", "npm run", "yarn run", "pnpm run", "bun run", "bash", "sh", "ssh", "zsh", "fish", "eval", "exec", "env", "xargs", "sudo")
NETWORK_CLIS = ("curl", "wget", "aws", "gcloud", "gsutil")
KUBECTL_MUTATING = ("exec", "apply", "delete", "patch", "edit", "replace", "scale", "rollout", "cp")
PS_LAUNCHERS = ("iex", "invoke-expression", "start-process")

#: Tools that write files (acceptEdits / sensitive-path checks).
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Read-only tool fast path in auto mode (04 §8: inferred membership).
READ_ONLY_FAST_PATH_TOOLS = frozenset({"Read", "Glob", "Grep", "TodoWrite", "ToolSearch", "NotebookRead"})

PLAN_MODE_MESSAGE = "Plan mode is active: only read-only actions can run until the plan is approved."
DONT_ASK_MESSAGE = "This session does not ask for permission, so actions that need approval are declined."


class ModeChangeError(Exception):
    """Distinct reasons: ``restricted`` | ``disabled`` | ``not_launched`` | ``cloud`` (04 R3)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


_MODE_CHANGE_TEXT = {
    "restricted": "Bypass mode is not available in restricted mode.",
    "disabled": "This mode has been disabled by your organisation's policy.",
    "not_launched": "Bypass mode can only be used if the session was started with --dangerously-skip-permissions.",
    "cloud": "Bypass mode is not available in cloud sessions.",
}


def _mode_disabled(settings: Settings, mode: PermissionMode) -> bool:
    """04 R2: policy keys switch modes off."""
    if mode == PermissionMode.BYPASS:
        return settings.get("permissions.disableBypassPermissionsMode") == "disable"
    if mode == PermissionMode.AUTO:
        return settings.get("permissions.disableAutoMode") == "disable"
    return False


def _parse_mode(value: Any) -> PermissionMode | None:
    try:
        return PermissionMode(value)
    except ValueError:
        return None


def initial_mode(settings: Settings, *, cli_mode: PermissionMode | None, dangerously_skip: bool) -> PermissionMode:
    """Resolve the starting mode honouring 04 R1/R2.

    Precedence: ``--dangerously-skip-permissions`` > ``--permission-mode`` >
    ``permissions.defaultMode`` from the highest-precedence settings layer -
    except that bypass from *project* or *local* settings (files that live in
    the repository) is skipped as if unset (R1). A disabled mode falls back to
    ``default`` (R2).
    """
    mode: PermissionMode | None = None
    if dangerously_skip:
        mode = PermissionMode.BYPASS
    elif cli_mode is not None:
        mode = cli_mode
    else:
        for src in reversed([SettingsSource.USER, SettingsSource.PROJECT, SettingsSource.LOCAL, SettingsSource.FLAG, SettingsSource.POLICY]):
            m = _parse_mode(settings.get_from(src, "permissions.defaultMode"))
            if m is None:
                continue
            if m == PermissionMode.BYPASS and src in (SettingsSource.PROJECT, SettingsSource.LOCAL):
                continue  # repository-controlled: never disarms the user (R1, 04#T8)
            mode = m
            break
    if mode is None or mode == PermissionMode.BUBBLE or _mode_disabled(settings, mode):
        return PermissionMode.DEFAULT
    return mode


def set_mode(perm: PermissionContext, mode: PermissionMode, settings: Settings, *, is_cloud: bool = False) -> None:
    """Runtime mode switch (04 R3/R4); raises ``ModeChangeError``.

    Entering plan mode remembers the current mode in ``perm.pre_plan_mode``;
    ``exit_plan_mode`` restores it.
    """
    if mode == PermissionMode.BYPASS:
        if is_cloud:
            raise ModeChangeError("cloud", _MODE_CHANGE_TEXT["cloud"])
        if settings.get("permissions.restricted"):
            raise ModeChangeError("restricted", _MODE_CHANGE_TEXT["restricted"])
        if _mode_disabled(settings, mode):
            raise ModeChangeError("disabled", _MODE_CHANGE_TEXT["disabled"])
        if not perm.bypass_available:
            raise ModeChangeError("not_launched", _MODE_CHANGE_TEXT["not_launched"])
    elif _mode_disabled(settings, mode):
        raise ModeChangeError("disabled", _MODE_CHANGE_TEXT["disabled"])
    if mode == PermissionMode.PLAN and perm.mode != PermissionMode.PLAN:
        perm.pre_plan_mode = perm.mode
    elif mode != PermissionMode.PLAN:
        perm.pre_plan_mode = None
    perm.mode = mode


def exit_plan_mode(perm: PermissionContext) -> PermissionMode:
    """Leave plan mode, restoring the mode that was active before it (R4)."""
    if perm.mode == PermissionMode.PLAN:
        perm.mode = perm.pre_plan_mode or PermissionMode.DEFAULT
        perm.pre_plan_mode = None
    return perm.mode


def mode_change_message(err: ModeChangeError) -> str:
    return _MODE_CHANGE_TEXT.get(err.reason, str(err))


# ------------------------------------------------------------------ §4.6 allow-rule stripping


def _strips_in_auto(rule: Rule, settings: Settings) -> bool:
    """Would this allow rule let the model run arbitrary code without the judge?"""
    if rule.tool_name not in ("Bash", "PowerShell"):
        return False
    if settings.get("autoMode.classifyAllShell", False):
        return True
    c = (rule.content or "").rstrip(":*").strip()
    if c in ("", "*"):
        return True
    low = c.lower()
    if any(low == p or low.startswith(p + " ") or low.startswith(p + "*") for p in INTERPRETERS + PS_LAUNCHERS):
        return True
    words = c.split()
    if words and words[0] in NETWORK_CLIS and any(not w.startswith("-") for w in words[1:]):
        return True
    if words and words[0] == "kubectl" and any(v in words for v in KUBECTL_MUTATING):
        return True
    return False


def effective_allow_rules(rules: list[Rule], mode: PermissionMode, settings: Settings) -> list[Rule]:
    """Allow rules that stay active in ``mode`` (04 §4.6, R11). Only allow rules
    are filtered; deny and ask rules always pass through unchanged."""
    if mode != PermissionMode.AUTO:
        return list(rules)
    return [r for r in rules if r.behavior != "allow" or not _strips_in_auto(r, settings)]


# ------------------------------------------------------------------ acceptEdits


def working_dirs(ctx: SessionContext) -> list[str]:
    return [str(ctx.cwd)] + [str(p) for p in ctx.additional_dirs] + list(ctx.permissions.additional_directories)


def _inside(path: str, roots: list[str], cwd: str) -> bool:
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    for r in roots:
        rn = os.path.normpath(r)
        if full == rn or full.startswith(rn.rstrip(os.sep) + os.sep):
            return True
    return False


def accept_edits_allows(tool: Tool, input: dict[str, Any], ctx: SessionContext) -> bool:
    """File writes inside working dirs passing safety checks; shell whose base command is in
    ``hazards.ACCEPT_EDITS_SHELL_COMMANDS`` (04 §2.1)."""
    cwd = str(ctx.cwd)
    roots = working_dirs(ctx)
    if tool.name in EDIT_TOOLS or tool.kind == "edit":
        path = tool.get_path(input) or input.get("file_path") or input.get("notebook_path")
        return isinstance(path, str) and _inside(path, roots, cwd) and not is_sensitive_path(path, cwd=cwd)
    if tool.name == "Bash":
        cmd = input.get("command")
        if not isinstance(cmd, str):
            return False
        ast = parse_shell(cmd)
        if ast.kind != "ok" or ast.has_command_substitution or ast.has_process_substitution or ast.has_unquoted_expansion:
            return False
        home = ctx.env.get("HOME") or os.path.expanduser("~")
        for c in ast.commands:
            if c.name not in ACCEPT_EDITS_SHELL_COMMANDS or c.env or c.background:
                return False
            if c.name == "sed" and sed_has_write_or_exec(c):
                return False
            args = [a for a in c.argv[1:] if not a.startswith("-")]
            if c.name == "sed" and args:
                args = args[1:]  # first positional is the script
            for a in args:
                if not _inside(a, roots, cwd) or is_sensitive_path(a, cwd=cwd):
                    return False
                if c.name in ("rm", "rmdir") and is_dangerous_removal_target(a, cwd=cwd, home=home):
                    return False
            if any(r.is_output and not r.is_safe() and not _inside(r.target, roots, cwd) for r in c.redirects):
                return False
        return True
    return False


# ------------------------------------------------------------------ §4.4 resolve_ask


def _is_safety(d: Decision) -> bool:
    return isinstance(d.reason, SafetyCheckReason)


def _forced_prompt(tool: Tool, d: Decision) -> bool:
    """Asks that no mode may silently turn into allow (except auto's classifier
    rules): content ask rules, hook ask floors, tools needing a human."""
    return d.reason.type == "rule" or d.matched_ask_rule is not None or d.hook_ask_floor or tool.requires_user_interaction() or d.org_ceiling or d.workflow_consent


def resolve_ask(tool: Tool, input: dict[str, Any], d: Decision, ctx: SessionContext) -> Decision:
    """04 §4.4 ``resolve_ask``. May return ``ask`` meaning "prompt a human"."""
    m = ctx.permissions.mode
    if m in (PermissionMode.DEFAULT, PermissionMode.BUBBLE):
        return d
    if m == PermissionMode.DONT_ASK:
        return Decision.deny(DONT_ASK_MESSAGE, ModeReason(m))
    if m == PermissionMode.PLAN:
        if not _tool_read_only(tool, input):
            return Decision.ask(ModeReason(m), PLAN_MODE_MESSAGE, plan_floor=True)
        if ctx.settings.get("useAutoModeDuringPlan", True) and ctx.permissions.pre_plan_mode == PermissionMode.AUTO:
            return auto_mode(tool, input, d, ctx)
        return d
    if m == PermissionMode.ACCEPT_EDITS:
        if not _is_safety(d) and not _forced_prompt(tool, d) and accept_edits_allows(tool, input, ctx):
            return Decision.allow(ModeReason(m))
        return d
    if m == PermissionMode.BYPASS:
        # 04 §2.1: ask -> allow, except bypass-immune safety checks, tools that opt
        # out, and content ask rules / hook asks, which still prompt.
        b = breaker(d.reason.circuit_breaker) if isinstance(d.reason, SafetyCheckReason) else None
        if (b is not None and b.bypass_immune) or tool.opts_out_of_bypass or _forced_prompt(tool, d) or d.reason.type == "rule":
            return d
        if b is not None and b.host_person_only:
            return d
        return Decision.allow(ModeReason(m))
    if m == PermissionMode.AUTO:
        return auto_mode(tool, input, d, ctx)
    return d


def _tool_read_only(tool: Tool, input: dict[str, Any]) -> bool:
    try:
        return bool(tool.is_read_only(input))
    except Exception:
        return False


def _first_read_outside_workdirs(tool: Tool, input: dict[str, Any], ctx: SessionContext) -> bool:
    if tool.kind not in ("read", "search"):
        return False
    path = tool.get_path(input)
    return isinstance(path, str) and not _inside(path, working_dirs(ctx), str(ctx.cwd))


def read_only_fast_allow(tool: Tool, input: dict[str, Any], ctx: SessionContext) -> bool:
    """04#T4 read-only tool allowlist: a read-only tool (``is_read_only`` or the
    inferred allowlist) whose target lies inside the working directories needs
    no prompt in any mode. Reads outside them still ask (04 §4.4)."""
    if tool.name == "Bash":
        return False  # shell commands go through bash_safety's own read-only analysis
    if not (_tool_read_only(tool, input) or tool.name in READ_ONLY_FAST_PATH_TOOLS):
        return False
    path = tool.get_path(input)
    return not (isinstance(path, str) and not _inside(path, working_dirs(ctx), str(ctx.cwd)))


def _classifier_for(ctx: SessionContext) -> Any:
    if ctx.classifier is None and ctx.model is not None:
        ctx.classifier = TwoStageClassifier(ctx.model, fast_only=bool(ctx.settings.get("autoMode.fastOnly", False)))
    return ctx.classifier


def denial_tracker(ctx: SessionContext) -> DenialTracker:
    if not isinstance(ctx.denial_state, DenialTracker):
        ctx.denial_state = DenialTracker()
    return ctx.denial_state


def auto_mode(tool: Tool, input: dict[str, Any], d: Decision, ctx: SessionContext) -> Decision:
    """04 §4.4 ``auto_mode``: hard-coded denies, then mandatory human asks (never
    classified), then cheap fast paths, then the classifier with denial tracking."""
    b = breaker(d.reason.circuit_breaker) if isinstance(d.reason, SafetyCheckReason) else None
    if b is not None and b.auto_mode_deny:
        return Decision.deny(
            f"Blocked in auto mode: {d.reason.message}",  # type: ignore[union-attr]
            ClassifierReason(0, d.reason.message, fast_path="hardcoded"),  # type: ignore[union-attr]
        )
    mandatory = (
        (isinstance(d.reason, SafetyCheckReason) and (not d.reason.classifier_approvable or (b is not None and b.host_person_only)))
        or d.reason.type == "rule"
        or d.matched_ask_rule is not None
        or d.plan_floor
        or d.org_ceiling
        or tool.requires_user_interaction()
        or d.workflow_consent
        or _first_read_outside_workdirs(tool, input, ctx)
        or d.hook_ask_floor
    )
    if mandatory:
        return d
    if not _is_safety(d) and accept_edits_allows(tool, input, ctx):
        return Decision.allow(ClassifierReason(0, "would be allowed in acceptEdits", fast_path="acceptEdits"))
    if not _is_safety(d) and (_tool_read_only(tool, input) or tool.name in READ_ONLY_FAST_PATH_TOOLS):
        return Decision.allow(ClassifierReason(0, "read-only", fast_path="allowlist"))
    clf = _classifier_for(ctx)
    if clf is None:
        verdict = ClassifierVerdict(True, 0, reason="no classifier configured", unavailable=True)
    else:
        try:
            verdict = clf.classify(tool, input, ctx)
        except Exception as e:  # fail closed (R21)
            verdict = ClassifierVerdict(True, 0, reason=f"classifier error: {e}", unavailable=True)
    tracker = denial_tracker(ctx)
    out = tracker.record(verdict, headless=ctx.permissions.headless)
    if tracker.end_turn_reason:
        ctx.extra["end_turn_request"] = tracker.end_turn_reason
        tracker.end_turn_reason = None
    return out
