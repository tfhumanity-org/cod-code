"""The permission pipeline: hooks -> rules -> mode -> classifier -> human (WP3).

Implements spec 04 §4.1 ``decide`` / ``rule_pipeline`` and R5, R6 (set-aside
unknown keys downgrade allow to passthrough), R24/R35 (hook allow still subject
to deny rules, ask rules, safety checks, and the classifier in auto mode; hook
ask forces a prompt; hook deny ends the call), R30 (denial guidance;
headless asks become denies), R45 (``autoAllowBashIfSandboxed`` lets sandboxed
shell calls skip whole-tool ask rules), R47 (headless order:
PermissionRequest hook, then host prompter, else deny), R48 (session-level
approval caching, ``updatedPermissions`` applied to ``ctx.permissions``);
takeaways 04#T1, 04#T2; P17.

``make_can_use_tool(ctx)`` returns the ``interfaces.CanUseTool`` used by
``tools.pipeline``: it always resolves to ``allow`` or ``deny``.

Contract notes for other packages
---------------------------------
* PreToolUse hooks run *here* (04 §4.1 step 0), so ``tools.pipeline`` should not
  run them a second time. Their ``additionalContext`` is left in
  ``ctx.extra["pre_tool_hook_context"][tool_use_id]`` for the caller to surface.
* Every denial is recorded in ``ctx.extra["permission_outcomes"][tool_use_id]``
  (``rejected-by-user`` | ``blocked-by-permissions`` | ``automode-blocked`` |
  ``automode-unavailable`` | ``automode-parsing-error``): the harness-authored
  outcome lines the classifier projection uses (04 §4.5).
* A PreToolUse ``defer`` comes back as ``Decision("deny", HookReason(name,
  "defer"))`` - the convention ``tools.pipeline`` checks (03 R7).
* When auto mode must end the turn (04 R22/R23) the reason is placed in
  ``ctx.extra["end_turn_request"]`` for the loop.
* Shell tools get the WP3 shell analysis (``bash_safety.check_bash``) combined
  with their own ``check_permissions`` result - the stricter wins, so a tool
  cannot widen what the analysis decided.
* MCP org ceiling (§4.1): ``ctx.extra["mcp_permission_ceiling"][server] == "ask"``.
* Workflow command clamp (06 R37): ``ctx.extra["bash_command_clamp"]`` (``Bash(...)``
  patterns) - shell calls none of them covers are denied in every mode.
* Read-only fast path (04#T4): a read-only tool inside the working directories is
  allowed by the rule pipeline (``modes.read_only_fast_allow``), so no mode prompts it.

Design choices: unknown input keys "set aside" (R6) are detected as keys not
declared in the tool's ``input_schema.properties``. ``updatedPermissions`` with a
settings-file destination are written to ``<config>/settings.json`` (user),
``<cwd>/.claude/settings.json`` (project) or ``<cwd>/.claude/settings.local.json``
(local). The PermissionRequest hook gets the first chance at every human
prompt, not only in headless mode (R47 order is kept).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harnesslab.hooks.runner import common_payload
from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.interfaces import CanUseTool
from harnesslab.permissions import modes
from harnesslab.permissions.bash_safety import check_bash
from harnesslab.permissions.classifier import AUTO_MODE_AGENT_GUIDANCE
from harnesslab.permissions.hazards import breaker, is_agent_settings_file, is_sensitive_path
from harnesslab.permissions.rules import first_match, mcp_server_of
from harnesslab.permissions.types import (
    ClassifierReason,
    Decision,
    HookReason,
    ModeReason,
    OtherReason,
    PermissionAnswer,
    PermissionMode,
    PermissionPromptRequest,
    PermissionUpdate,
    Rule,
    RuleReason,
    RuleSource,
    SafetyCheckReason,
    SubcommandResultsReason,
    WorkingDirReason,
)
from harnesslab.pseudo.sandbox_profiles import SandboxDecision, sandbox_decision
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext

# ------------------------------------------------------------------ denial texts (own wording, R30)

DENIAL_GUIDANCE = (
    "This action did not run, so do not say or imply that it succeeded. You may keep working with "
    "other legitimate tools, but do not try to get around this decision - for example by splitting "
    "the command, rewording it, or reaching the same effect through another tool. If this action is "
    "essential to the task, stop and explain to the user what you need and why."
)
HEADLESS_MESSAGE = "Permission prompts are not available in this session, so this action was declined automatically."
USER_REJECTED_MESSAGE = "The user declined this action."
USER_REJECTION_REASON = "rejected by the user"


def with_guidance(message: str | None, *, auto_mode: bool = False) -> str:
    """Append the R30 do-not-circumvent guidance (and the R29 auto-mode hint)."""
    msg = (message or "Permission denied.").strip()
    if DENIAL_GUIDANCE not in msg:
        msg = f"{msg}\n\n{DENIAL_GUIDANCE}"
    if auto_mode and AUTO_MODE_AGENT_GUIDANCE not in msg:
        msg = f"{msg}\n\n{AUTO_MODE_AGENT_GUIDANCE}"
    return msg


# ------------------------------------------------------------------ helpers


def _is_shell(tool: Tool) -> bool:
    return tool.name in ("Bash",)


def _home(ctx: SessionContext) -> str:
    return ctx.env.get("HOME") or os.path.expanduser("~")


def _has_set_aside_keys(tool: Tool, input: dict[str, Any]) -> bool:
    props = (tool.input_schema or {}).get("properties") or {}
    return bool(props) and any(k not in props for k in input)


def _combine_tool_and_shell(tool_d: Decision, shell_d: Decision) -> Decision:
    """The tool's own check and the shell analysis: the stricter one wins.
    ``passthrough`` from the tool means "no opinion"."""
    if tool_d.behavior == "passthrough":
        return shell_d
    return tool_d if tool_d.severity > shell_d.severity else shell_d


def _sandbox_for(tool: Tool, input: dict[str, Any], ctx: SessionContext) -> SandboxDecision | None:
    if not _is_shell(tool) or not isinstance(input.get("command"), str):
        return None
    return sandbox_decision(input["command"], input, ctx.settings.get("sandbox", {}) or {})


def _is_rule_ask(d: Decision) -> bool:
    return d.behavior == "ask" and (d.reason.type == "rule" or d.matched_ask_rule is not None)


def _sensitive_write_filter(tool: Tool, input: dict[str, Any], d: Decision, ctx: SessionContext) -> Decision:
    """R18: writes to sensitive paths raise a safety check (classifier-approvable,
    still prompting in acceptEdits); the agent's own settings file is human-only."""
    if d.behavior not in ("allow", "passthrough"):
        return d
    if not (tool.name in modes.EDIT_TOOLS or tool.kind == "edit"):
        return d
    path = tool.get_path(input) or input.get("file_path") or input.get("notebook_path")
    if not isinstance(path, str):
        return d
    cwd = str(ctx.cwd)
    if is_agent_settings_file(path, cwd=cwd):
        out = Decision.ask(
            SafetyCheckReason(f"{path} holds the agent's own permission settings.", classifier_approvable=False, circuit_breaker="claudeSettingsFile")
        )
        out.blocked_path = path
        return out
    if is_sensitive_path(path, cwd=cwd):
        out = Decision.ask(SafetyCheckReason(f"{path} is a sensitive configuration path.", classifier_approvable=True))
        out.blocked_path = path
        return out
    return d


def _sandbox_escape_filter(tool: Tool, input: dict[str, Any], d: Decision, ctx: SessionContext) -> Decision:
    """R45: an honoured ``dangerouslyDisableSandbox`` always asks."""
    sb = _sandbox_for(tool, input, ctx)
    if sb is not None and sb.force_ask and d.behavior != "deny":
        return Decision.ask(OtherReason("command asks to run outside the sandbox"), "This command would run outside the sandbox.")
    return d


POST_FILTERS = (_sensitive_write_filter, _sandbox_escape_filter)


# ------------------------------------------------------------------ rule pipeline


def rule_pipeline(tool: Tool, input: dict[str, Any], call_ctx: ToolCallContext) -> Decision:
    """04 §4.1 ``rule_pipeline``: deterministic layer. Returns allow, deny or ask
    (never passthrough). Deny > ask > allow regardless of which source a rule
    came from."""
    ctx = call_ctx.session
    perm = ctx.permissions
    cwd = str(ctx.cwd)
    deny_rules = perm.rules_with("deny")
    ask_rules = perm.rules_with("ask")
    allow_rules = modes.effective_allow_rules(perm.rules_with("allow"), perm.mode, ctx.settings)
    sb = _sandbox_for(tool, input, ctx)

    # 1. deny rules (whole tool or content; any subcommand of a shell line)
    if (r := first_match(deny_rules, tool, input, cwd=cwd)) is not None:
        return Decision.deny(f"Permission to use {tool.name} was denied by the rule {r}.", RuleReason(r))
    # 1b. workflow command clamp (06 R37): a clamped agent may only run shell
    #     commands one of its Bash(...) patterns covers - fail closed otherwise
    clamp = ctx.extra.get("bash_command_clamp")
    if clamp and _is_shell(tool):
        from harnesslab.permissions.rules import parse_rule

        clamp_rules = [parse_rule(str(c), "allow") for c in clamp]
        if first_match(clamp_rules, tool, input, cwd=cwd) is None:
            return Decision.deny(
                f"This agent may only run shell commands matching {', '.join(map(str, clamp))}.",
                OtherReason("outside the workflow's bash command clamp"),
            )
    # 2. whole-tool ask rules (sandboxed shell calls may skip them, R45)
    if (r := first_match(ask_rules, tool, input, cwd=cwd, whole_only=True)) is not None:
        if not (sb is not None and sb.sandboxed and sb.auto_allow):
            return Decision.ask(RuleReason(r), matched_ask_rule=r)
    # 3. tool-specific checks (+ shell analysis for shell tools)
    try:
        d = tool.check_permissions(input, call_ctx)
    except Exception as e:  # a crashing check must not become an allow
        d = Decision.ask(OtherReason(f"permission check failed: {e}"))
    if _is_shell(tool) and isinstance(input.get("command"), str):
        shell_d = check_bash(
            input["command"], perm, cwd=cwd, home=_home(ctx), allow_rules=allow_rules,
            working_dirs=[str(p) for p in ctx.additional_dirs],
        )
        d = _combine_tool_and_shell(d, shell_d)
    if d.behavior == "deny":
        return d
    # 3b. read-only tool fast path (04#T4): a read-only tool inside the working
    #     directories is allowed by its own check, so default mode does not prompt
    if d.behavior == "passthrough" and modes.read_only_fast_allow(tool, input, ctx):
        d = Decision.allow(OtherReason("read-only tool inside the working directories"))
    # 4. R6: unknown keys set aside -> the tool's allow is not trustworthy
    if d.behavior == "allow" and _has_set_aside_keys(tool, input):
        d = Decision.passthrough(OtherReason("input contained unrecognised keys; check incomplete"))
    # 5. post filters (sensitive paths, sandbox escape)
    for post in POST_FILTERS:
        d = post(tool, input, d, ctx)
        if d.behavior == "deny":
            return d
    # 6. content ask rules
    if (r := first_match(ask_rules, tool, input, cwd=cwd, content_only=True)) is not None:
        if d.behavior != "ask":
            d = Decision.ask(RuleReason(r), matched_ask_rule=r, suggestions=d.suggestions)
        else:
            d.matched_ask_rule = r
    # 7. tools that always need a human
    if tool.requires_user_interaction():
        if d.behavior != "ask":
            d = Decision.ask(d.reason, d.message)
        return d
    # 8. organisation ceiling for MCP servers
    if tool.is_mcp and (ctx.extra.get("mcp_permission_ceiling") or {}).get(mcp_server_of(tool.name) or "") == "ask":
        return Decision.ask(OtherReason("organisation policy requires approval for this MCP server"), org_ceiling=True)
    if d.behavior == "ask":
        return d
    # 10. bypass: everything else is allowed (asks were returned above and are
    #     resolved by modes.resolve_ask, which honours bypass-immune breakers)
    if perm.mode == PermissionMode.BYPASS and not tool.opts_out_of_bypass:
        return Decision.allow(ModeReason(PermissionMode.BYPASS))
    if d.behavior == "allow":
        return d
    if sb is not None and sb.sandboxed and sb.auto_allow:
        return Decision.allow(OtherReason("sandboxed shell command (autoAllowBashIfSandboxed)"))
    # 12. allow rules
    if (r := first_match(allow_rules, tool, input, cwd=cwd)) is not None:
        return Decision.allow(RuleReason(r))
    out = Decision.ask(d.reason, d.message, suggestions=d.suggestions or _default_suggestions(tool, input))
    out.blocked_path = d.blocked_path
    return out


def _default_suggestions(tool: Tool, input: dict[str, Any]) -> list[PermissionUpdate]:
    """Rules a UI may offer to save ("always allow")."""
    if tool.is_mcp or tool.name.startswith("mcp__"):
        return [PermissionUpdate("addRules", "localSettings", [Rule(tool.name)], "allow")]
    if tool.name in modes.EDIT_TOOLS:
        return [PermissionUpdate("setMode", "session", mode=PermissionMode.ACCEPT_EDITS)]
    return [PermissionUpdate("addRules", "localSettings", [Rule(tool.name)], "allow")]


# ------------------------------------------------------------------ decide (hooks + rules + mode)


def _run_pre_tool_hooks(tool: Tool, input: dict[str, Any], call_ctx: ToolCallContext) -> AggregatedHookResult | None:
    ctx = call_ctx.session
    if ctx.hook_runner is None:
        return None
    fields: dict[str, Any] = {
        "tool_name": tool.name,
        "tool_input": input,
        "tool_use_id": call_ctx.tool_use_id,
        "tool_aliases": list(tool.aliases),
    }
    server = mcp_server_of(tool.name)
    if server:
        fields["mcp_server"] = {"name": server, "source": getattr(tool, "mcp_source", "unknown")}
    agg = ctx.hook_runner.run(HookEvent.PRE_TOOL_USE, common_payload(HookEvent.PRE_TOOL_USE, ctx, **fields), ctx)
    if agg.additional_contexts:
        ctx.extra.setdefault("pre_tool_hook_context", {})[call_ctx.tool_use_id] = list(agg.additional_contexts)
    return agg


def decide(tool: Tool, input: dict[str, Any], call_ctx: ToolCallContext) -> Decision:
    """Hooks (PreToolUse) + ``rule_pipeline`` + ``modes.resolve_ask``. May return ask.

    Monotonic layering (04 R35, 04#T2): a hook deny ends the call; a hook allow
    is re-checked against deny rules, ask rules and safety checks, and in auto
    mode still goes to the classifier (R24); a hook ask forces a prompt.
    """
    ctx = call_ctx.session
    h = _run_pre_tool_hooks(tool, input, call_ctx)
    if h is not None and h.decision == "defer" and not h.blocked:
        # 03 R7 / 04 §8: the tool pipeline honours this only headless and alone in
        # its batch (pausing the turn); everywhere else it stands as a deny.
        msg = h.decision_reason or "A PreToolUse hook deferred this call to the host."
        return Decision.deny(msg, HookReason(h.hook_name or "PreToolUse", "defer"))
    if h is not None and (h.blocked or h.decision == "deny"):
        msg = h.decision_reason or "; ".join(h.blocking_errors) or "Blocked by a PreToolUse hook."
        return Decision.deny(msg, HookReason(h.hook_name or "PreToolUse", msg))
    hook_rewrote = h is not None and h.updated_input is not None
    if hook_rewrote:
        input = dict(h.updated_input)  # type: ignore[arg-type]

    d = rule_pipeline(tool, input, call_ctx)

    if h is not None and h.decision == "allow":
        reason = HookReason(h.hook_name or "PreToolUse", h.decision_reason)
        overridden = (
            d.behavior == "deny"
            or _is_rule_ask(d)
            or isinstance(d.reason, SafetyCheckReason)
            or (tool.requires_user_interaction() and not hook_rewrote)
        )
        if not overridden:
            if ctx.permissions.mode == PermissionMode.AUTO:
                d = Decision.ask(reason, suggestions=d.suggestions)  # funnel to the classifier
            else:
                return Decision.allow(reason, updated_input=input)
    if h is not None and h.decision == "ask" and d.behavior != "deny":
        d = Decision.ask(HookReason(h.hook_name or "PreToolUse", h.decision_reason), h.decision_reason or None, hook_ask_floor=True, suggestions=d.suggestions)

    if d.behavior == "ask":
        d = modes.resolve_ask(tool, input, d, ctx)
    if hook_rewrote and d.updated_input is None:
        d.updated_input = input
    return d


# ------------------------------------------------------------------ host prompt


def describe_reason(d: Decision) -> str:
    """Human/host text for a typed reason (04#T1: one discriminator drives UI text)."""
    r = d.reason
    if isinstance(r, RuleReason):
        return f"Matches {r.rule.behavior} rule {r.rule} (from {r.rule.source.value})"
    if isinstance(r, ModeReason):
        return d.message or f"Permission mode {r.mode.value}"
    if isinstance(r, HookReason):
        return f"Hook {r.hook_name}: {r.reason}" if r.reason else f"Hook {r.hook_name} asked for confirmation"
    if isinstance(r, ClassifierReason):
        return f"Auto mode reviewer: {r.reason}" if r.reason else "Auto mode reviewer"
    if isinstance(r, SafetyCheckReason):
        return r.message
    if isinstance(r, SubcommandResultsReason):
        return "; ".join(describe_reason(x) for x in r.per_subcommand if x.behavior != "allow")
    if isinstance(r, WorkingDirReason):
        return f"{r.path} is outside the allowed working directories"
    return getattr(r, "text", "") or d.message or ""


def _describe_call(tool: Tool, input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "url", "query", "pattern"):
        v = input.get(key)
        if isinstance(v, str):
            return v
    return json.dumps(input, sort_keys=True, default=str)[:500]


def prompt_request(tool: Tool, input: dict[str, Any], d: Decision, call_ctx: ToolCallContext) -> PermissionPromptRequest:
    """Build the rich host payload (04 §2.10, 04#T12)."""
    safety = d.reason if isinstance(d.reason, SafetyCheckReason) else None
    b = breaker(safety.circuit_breaker) if safety else None
    return PermissionPromptRequest(
        tool_name=tool.name,
        input=input,
        tool_use_id=call_ctx.tool_use_id,
        decision=d,
        agent_id=call_ctx.session.agent_id,
        permission_suggestions=list(d.suggestions),
        blocked_path=d.blocked_path,
        decision_reason_text=describe_reason(d),
        classifier_approvable=bool(safety and safety.classifier_approvable),
        suppress_always_allow_rule=safety is not None or d.hook_ask_floor or d.plan_floor,
        default_to_no=bool(b and (b.bypass_immune or b.host_person_only)),
        requires_user_interaction=tool.requires_user_interaction(),
        title=f"Allow {tool.name}?",
        description=_describe_call(tool, input),
    )


# ------------------------------------------------------------------ updates


_SETTINGS_KEY = {"allow": "allow", "ask": "ask", "deny": "deny"}


def _settings_file(ctx: SessionContext, destination: str) -> Path | None:
    if destination == "userSettings":
        return ctx.config_dir / "settings.json"
    if destination == "projectSettings":
        return Path(ctx.cwd) / ".claude" / "settings.json"
    if destination == "localSettings":
        return Path(ctx.cwd) / ".claude" / "settings.local.json"
    return None


def _persist(ctx: SessionContext, u: PermissionUpdate) -> None:
    path = _settings_file(ctx, u.destination)
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return  # never overwrite a file we could not read
    perms = data.setdefault("permissions", {})
    if u.type in ("addRules", "replaceRules", "removeRules"):
        key = _SETTINGS_KEY[u.behavior]
        current = [] if u.type == "replaceRules" else list(perms.get(key, []))
        texts = [str(r) for r in u.rules]
        if u.type == "removeRules":
            current = [t for t in current if t not in texts]
        else:
            current += [t for t in texts if t not in current]
        perms[key] = current
    elif u.type == "setMode" and u.mode is not None:
        perms["defaultMode"] = u.mode.value
    elif u.type in ("addDirectories", "removeDirectories"):
        dirs = list(perms.get("additionalDirectories", []))
        if u.type == "addDirectories":
            dirs += [d for d in u.directories if d not in dirs]
        else:
            dirs = [d for d in dirs if d not in u.directories]
        perms["additionalDirectories"] = dirs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def apply_updates(ctx: SessionContext, updates: list[PermissionUpdate]) -> None:
    """Apply ``updatedPermissions`` (session destination in memory; others written to the
    settings file of that destination)."""
    perm = ctx.permissions
    for u in updates:
        if u.type in ("addRules", "replaceRules", "removeRules"):
            src = RuleSource(u.destination)
            new = [Rule(r.tool_name, r.content, u.behavior, src) for r in u.rules]
            if u.type == "replaceRules":
                perm.rules = [r for r in perm.rules if not (r.behavior == u.behavior and r.source == src)]
            if u.type == "removeRules":
                keys = {(r.tool_name, r.content) for r in new}
                perm.rules = [r for r in perm.rules if not (r.behavior == u.behavior and (r.tool_name, r.content) in keys)]
            else:
                perm.rules += [r for r in new if r not in perm.rules]
        elif u.type == "setMode" and u.mode is not None:
            try:
                modes.set_mode(perm, u.mode, ctx.settings)
            except modes.ModeChangeError:
                continue  # an approval can never unlock a forbidden mode
        elif u.type == "addDirectories":
            perm.additional_directories += [d for d in u.directories if d not in perm.additional_directories]
        elif u.type == "removeDirectories":
            perm.additional_directories = [d for d in perm.additional_directories if d not in u.directories]
        _persist(ctx, u)


def answer_to_decision(answer: PermissionAnswer, input: dict[str, Any]) -> Decision:
    if answer.behavior == "allow":
        return Decision.allow(OtherReason("approved by the user"), updated_input=answer.updated_input or input)
    return Decision.deny(answer.message or USER_REJECTED_MESSAGE, OtherReason(USER_REJECTION_REASON))


def _suggestions_payload(updates: list[PermissionUpdate]) -> list[dict[str, Any]]:
    return [
        {"type": u.type, "rules": [str(r) for r in u.rules], "behavior": u.behavior, "destination": u.destination, **({"mode": u.mode.value} if u.mode else {})}
        for u in updates
    ]


def _ask_human(tool: Tool, input: dict[str, Any], d: Decision, call_ctx: ToolCallContext) -> Decision:
    """R47 order: PermissionRequest hook -> host/user prompter -> deny (headless)."""
    ctx = call_ctx.session
    if ctx.hook_runner is not None:
        payload = common_payload(
            HookEvent.PERMISSION_REQUEST, ctx, tool_name=tool.name, tool_input=input,
            tool_use_id=call_ctx.tool_use_id, tool_aliases=list(tool.aliases),
            permission_suggestions=_suggestions_payload(d.suggestions),
        )
        agg = ctx.hook_runner.run(HookEvent.PERMISSION_REQUEST, payload, ctx)
        if agg.decision == "deny" or agg.blocked:
            msg = agg.decision_reason or "; ".join(agg.blocking_errors) or "Declined by a PermissionRequest hook."
            return Decision.deny(msg, HookReason(agg.hook_name or "PermissionRequest", msg))
        if agg.decision == "allow":
            return Decision.allow(HookReason(agg.hook_name or "PermissionRequest", agg.decision_reason), updated_input=agg.updated_input or input)
    if ctx.prompter is not None:
        try:
            answer = ctx.prompter(prompt_request(tool, input, d, call_ctx))
        except Exception as e:  # a broken prompt channel fails closed
            return Decision.deny(f"Could not ask for permission: {e}", d.reason)
        if answer.behavior == "allow" and answer.updated_permissions:
            apply_updates(ctx, answer.updated_permissions)
        if answer.interrupt:
            ctx.abort.abort("interrupted at permission prompt")
        return answer_to_decision(answer, input)
    return Decision.deny(HEADLESS_MESSAGE, d.reason)


def _outcome_label(d: Decision) -> str:
    r = d.reason
    if isinstance(r, OtherReason) and r.text == USER_REJECTION_REASON:
        return "rejected-by-user"
    if isinstance(r, ClassifierReason) and r.fast_path != "hardcoded":
        if r.reason == "parsing-error":
            return "automode-parsing-error"
        if "unavailable" in r.reason:
            return "automode-unavailable"
        return "automode-blocked"
    return "blocked-by-permissions"


def make_can_use_tool(ctx: SessionContext) -> CanUseTool:
    """The ``interfaces.CanUseTool`` for a session: always resolves to allow or deny."""

    def can_use_tool(tool: Tool, input: dict[str, Any], call_ctx: ToolCallContext) -> Decision:
        session = call_ctx.session if call_ctx.session is not None else ctx
        d = decide(tool, input, call_ctx)
        if d.behavior in ("ask", "passthrough"):
            d = _ask_human(tool, d.updated_input or input, d, call_ctx)
        if d.behavior == "deny":
            session.extra.setdefault("permission_outcomes", {})[call_ctx.tool_use_id] = _outcome_label(d)
            d.message = with_guidance(d.message, auto_mode=isinstance(d.reason, ClassifierReason))
        call_ctx.permission_decision = d
        return d

    return can_use_tool
