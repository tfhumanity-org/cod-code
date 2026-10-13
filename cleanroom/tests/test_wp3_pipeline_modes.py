"""WP3 permission pipeline + modes tests (spec 04 R1-R11, R18-R19, R24, R30,
R35, R45, R47-R48, §4.1, §4.4, §4.6; 04#T1-T3, T8)."""

from __future__ import annotations

import json

import pytest

from harnesslab.config import Settings, SettingsSource
from harnesslab.hooks.runner import HookRunner
from harnesslab.hooks.types import HookEvent, HookMatcherGroup, HookSpec
from harnesslab.permissions import modes
from harnesslab.permissions.pipeline import (
    DENIAL_GUIDANCE,
    HEADLESS_MESSAGE,
    apply_updates,
    decide,
    make_can_use_tool,
    prompt_request,
)
from harnesslab.permissions.types import (
    ClassifierReason,
    ClassifierVerdict,
    HookReason,
    ModeReason,
    PermissionMode,
    PermissionUpdate,
    Rule,
    RuleReason,
    RuleSource,
    SafetyCheckReason,
)
from tests.wp3_fakes import (
    AllowingTool,
    FakeBash,
    FakeEdit,
    FakeMcp,
    FakeRead,
    InteractiveTool,
    RecordingPrompter,
    call_ctx,
    rule,
)

BASH, EDIT, READ = FakeBash(), FakeEdit(), FakeRead()


class FakeClassifier:
    def __init__(self, *verdicts: ClassifierVerdict) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[str] = []

    def classify(self, tool, input, ctx):
        self.calls.append(tool.name)
        return self.verdicts.pop(0) if self.verdicts else ClassifierVerdict(False, 1)


@pytest.fixture
def run(ctx):
    """``run(tool, input)`` -> final Decision through ``make_can_use_tool``."""
    ctx.prompter = RecordingPrompter("allow")
    cut = make_can_use_tool(ctx)

    def _run(tool, input, tid="tu1"):
        return cut(tool, input, call_ctx(ctx, tid))

    return _run


def hook(ctx, decision: str, event=HookEvent.PRE_TOOL_USE, **extra):
    out = {"hookSpecificOutput": {"permissionDecision": decision, "permissionDecisionReason": f"hook says {decision}", **extra}}
    runner = ctx.hook_runner or HookRunner()
    runner.register(event, HookMatcherGroup(None, [HookSpec("callback", callback=lambda p: out)]))
    ctx.hook_runner = runner


# ---------------------------------------------------------------- precedence (§2.3)


def test_deny_beats_ask_beats_allow_regardless_of_source(ctx, run):
    ctx.permissions.rules = [
        rule("Bash(git:*)", "allow", RuleSource.POLICY),
        rule("Bash(git push:*)", "deny", RuleSource.SESSION),
        rule("Bash(git commit:*)", "ask", RuleSource.LOCAL),
    ]
    d = run(BASH, {"command": "git push origin main"})
    assert d.behavior == "deny" and isinstance(d.reason, RuleReason) and DENIAL_GUIDANCE in d.message
    d = decide(BASH, {"command": "git commit -m x"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.matched_ask_rule is not None
    assert decide(BASH, {"command": "git add ."}, call_ctx(ctx)).behavior == "allow"


def test_whole_tool_rules_and_default_ask(ctx, run):
    assert decide(EDIT, {"file_path": "a.py"}, call_ctx(ctx)).behavior == "ask"
    ctx.permissions.rules = [rule("Edit")]
    assert decide(EDIT, {"file_path": "a.py"}, call_ctx(ctx)).behavior == "allow"
    ctx.permissions.rules = [rule("Edit"), rule("Edit", "deny")]
    assert run(EDIT, {"file_path": "a.py"}).behavior == "deny"


def test_read_only_bash_needs_no_rule(ctx, run):
    d = run(BASH, {"command": "git log --oneline | head"})
    assert d.behavior == "allow" and not ctx.prompter.requests


def test_mcp_rules_in_pipeline(ctx):
    ctx.permissions.rules = [rule("mcp__github")]
    assert decide(FakeMcp(), {"title": "x"}, call_ctx(ctx)).behavior == "allow"
    ctx.extra["mcp_permission_ceiling"] = {"github": "ask"}
    d = decide(FakeMcp(), {"title": "x"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.org_ceiling


# ---------------------------------------------------------------- hooks interplay (R35, R24)


def test_hook_deny_ends_the_call(ctx, run):
    ctx.permissions.rules = [rule("Bash")]
    hook(ctx, "deny")
    d = run(BASH, {"command": "ls"})
    assert d.behavior == "deny" and isinstance(d.reason, HookReason)


def test_hook_allow_still_subject_to_deny_rule_and_safety_checks(ctx, run):
    hook(ctx, "allow")
    ctx.permissions.rules = [rule("Bash(rm:*)", "deny")]
    assert run(BASH, {"command": "rm x"}).behavior == "deny"
    # safety check (sensitive path) still prompts despite the hook allow
    d = decide(EDIT, {"file_path": ".git/config"}, call_ctx(ctx))
    assert d.behavior == "ask" and isinstance(d.reason, SafetyCheckReason)
    # without a reason to override, a hook allow allows (no prompt)
    d = run(EDIT, {"file_path": "src/a.py"})
    assert d.behavior == "allow" and isinstance(d.reason, HookReason) and not ctx.prompter.requests


def test_hook_allow_overridden_by_ask_rule(ctx):
    hook(ctx, "allow")
    ctx.permissions.rules = [rule("Edit(src/**)", "ask")]
    d = decide(EDIT, {"file_path": "src/a.py"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.matched_ask_rule is not None


def test_hook_ask_forces_prompt_even_with_allow_rule(ctx, run):
    ctx.permissions.rules = [rule("Bash")]
    hook(ctx, "ask")
    d = decide(BASH, {"command": "ls"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.hook_ask_floor
    run(BASH, {"command": "ls"})
    assert ctx.prompter.requests and ctx.prompter.requests[0].suppress_always_allow_rule


def test_hook_allow_in_auto_mode_still_goes_to_classifier(ctx):
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.classifier = FakeClassifier(ClassifierVerdict(True, 2, reason="nope"))
    hook(ctx, "allow")
    d = decide(BASH, {"command": "make deploy"}, call_ctx(ctx))
    assert ctx.classifier.calls == ["Bash"] and d.behavior == "deny"


def test_hook_updated_input_is_used_and_satisfies_interaction(ctx, run):
    hook(ctx, "allow", updatedInput={"question": "rewritten"})
    d = run(InteractiveTool(), {"question": "q"})
    assert d.behavior == "allow" and d.updated_input == {"question": "rewritten"}


def test_requires_user_interaction_prompts_even_with_allow_rule(ctx, run):
    ctx.permissions.rules = [rule("AskUser")]
    d = run(InteractiveTool(), {"question": "q"})
    assert d.behavior == "allow" and len(ctx.prompter.requests) == 1
    assert ctx.prompter.requests[0].requires_user_interaction


def test_set_aside_keys_downgrade_tool_allow(ctx, run):
    assert decide(AllowingTool(), {"x": 1}, call_ctx(ctx)).behavior == "allow"
    d = decide(AllowingTool(), {"x": 1, "unexpected": 2}, call_ctx(ctx))
    assert d.behavior == "ask"


# ---------------------------------------------------------------- modes (§2.1, §4.4)


def test_default_mode_prompts_and_answer_applied(ctx, run):
    ctx.prompter = RecordingPrompter(
        "allow", updated_permissions=[PermissionUpdate("addRules", "session", [Rule("Bash", "make:*")], "allow")]
    )
    assert run(BASH, {"command": "make build"}).behavior == "allow"
    assert len(ctx.prompter.requests) == 1
    assert run(BASH, {"command": "make test"}).behavior == "allow"
    assert len(ctx.prompter.requests) == 1  # session rule now covers it


def test_user_rejection_recorded(ctx, run):
    ctx.prompter = RecordingPrompter("deny", message="not now")
    d = run(BASH, {"command": "make"}, tid="t9")
    assert d.behavior == "deny" and d.message.startswith("not now")
    assert ctx.extra["permission_outcomes"]["t9"] == "rejected-by-user"


def test_dont_ask_mode_denies(ctx, run):
    ctx.permissions.mode = PermissionMode.DONT_ASK
    d = run(BASH, {"command": "make"})
    assert d.behavior == "deny" and isinstance(d.reason, ModeReason) and not ctx.prompter.requests


def test_accept_edits_mode(ctx, run, project):
    ctx.permissions.mode = PermissionMode.ACCEPT_EDITS
    assert run(EDIT, {"file_path": str(project / "a.py")}).behavior == "allow"
    assert run(BASH, {"command": "mkdir -p out && touch out/x"}).behavior == "allow"
    assert not ctx.prompter.requests
    run(EDIT, {"file_path": str(project / ".git" / "config")})  # sensitive -> still prompts (R18)
    run(EDIT, {"file_path": "/etc/hosts"})  # outside working dir
    run(BASH, {"command": "curl example.com"})
    assert len(ctx.prompter.requests) == 3


def test_plan_mode_blocks_non_read_only(ctx, run):
    ctx.permissions.mode = PermissionMode.PLAN
    d = decide(BASH, {"command": "make"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.plan_floor and "Plan mode" in d.message
    assert run(BASH, {"command": "ls"}).behavior == "allow"


def test_bypass_mode_allows_but_immune_breakers_prompt(ctx, run):
    ctx.permissions.mode = PermissionMode.BYPASS
    assert run(BASH, {"command": "make deploy"}).behavior == "allow"
    assert not ctx.prompter.requests
    d = decide(BASH, {"command": "rm -rf $UNSET/"}, call_ctx(ctx))
    assert d.behavior == "ask" and d.reason.circuit_breaker == "dangerousRemoval"
    # non-immune safety check (background &) is allowed in bypass
    assert decide(BASH, {"command": "make &"}, call_ctx(ctx)).behavior == "allow"
    # deny rules and content ask rules still apply
    ctx.permissions.rules = [rule("Bash(make:*)", "deny"), rule("Bash(npm:*)", "ask")]
    assert decide(BASH, {"command": "make"}, call_ctx(ctx)).behavior == "deny"
    assert decide(BASH, {"command": "npm publish"}, call_ctx(ctx)).behavior == "ask"
    req = prompt_request(BASH, {"command": "rm -rf $UNSET/"}, decide(BASH, {"command": "rm -rf $UNSET/"}, call_ctx(ctx)), call_ctx(ctx))
    assert req.default_to_no and req.suppress_always_allow_rule


def test_headless_ask_becomes_deny_with_guidance(ctx):
    ctx.prompter = None
    d = make_can_use_tool(ctx)(BASH, {"command": "make"}, call_ctx(ctx))
    assert d.behavior == "deny" and d.message.startswith(HEADLESS_MESSAGE) and DENIAL_GUIDANCE in d.message


def test_permission_request_hook_gets_first_chance(ctx, run):
    runner = HookRunner()
    runner.register(
        HookEvent.PERMISSION_REQUEST,
        HookMatcherGroup("Bash", [HookSpec("callback", callback=lambda p: {"hookSpecificOutput": {"decision": {"behavior": "allow"}}})]),
    )
    ctx.hook_runner = runner
    assert run(BASH, {"command": "make"}).behavior == "allow"
    assert not ctx.prompter.requests


# ---------------------------------------------------------------- auto mode (R11, R20, §4.4, §4.6)


def test_auto_mode_fast_paths_and_classifier(ctx, run, project):
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.classifier = FakeClassifier(ClassifierVerdict(False, 1), ClassifierVerdict(True, 2, category="destructive-vcs", reason="force push"))
    d = run(EDIT, {"file_path": str(project / "a.py")})
    assert d.behavior == "allow" and d.reason.fast_path == "acceptEdits"
    assert run(READ, {"file_path": str(project / "x")}).behavior == "allow"
    assert ctx.classifier.calls == []
    assert run(BASH, {"command": "make build"}).behavior == "allow"
    d = run(BASH, {"command": "git push --force"}, tid="t2")
    assert d.behavior == "deny" and isinstance(d.reason, ClassifierReason)
    assert ctx.extra["permission_outcomes"]["t2"] == "automode-blocked"
    assert ctx.classifier.calls == ["Bash", "Bash"]
    assert not ctx.prompter.requests


def test_auto_mode_hard_denies_dangerous_removal(ctx, run):
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.classifier = FakeClassifier()
    d = run(BASH, {"command": "rm -rf ~"})
    assert d.behavior == "deny" and d.reason.fast_path == "hardcoded" and not ctx.classifier.calls


def test_auto_mode_mandatory_asks_skip_classifier(ctx, run):
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.classifier = FakeClassifier()
    ctx.permissions.rules = [rule("Bash(npm publish:*)", "ask")]
    run(BASH, {"command": "npm publish"})
    run(READ, {"file_path": "/etc/shadow"})  # read outside working dirs
    assert not ctx.classifier.calls and len(ctx.prompter.requests) == 2


def test_auto_mode_strips_broad_allow_rules(ctx):
    settings = Settings()
    rules = [rule("Bash(python:*)"), rule("Bash(npm run *)"), rule("Bash"), rule("Bash(curl https://x.io)"), rule("Bash(curl)"),
             rule("Bash(kubectl apply:*)"), rule("Bash(git status:*)"), rule("Edit"), rule("Bash(python:*)", "deny")]
    kept = modes.effective_allow_rules(rules, PermissionMode.AUTO, settings)
    assert [str(r) for r in kept if r.behavior == "allow"] == ["Bash(curl)", "Bash(git status:*)", "Edit"]
    assert rule("Bash(python:*)", "deny") in kept
    assert modes.effective_allow_rules(rules, PermissionMode.DEFAULT, settings) == rules
    all_shell = Settings.from_dict({"autoMode": {"classifyAllShell": True}})
    assert [str(r) for r in modes.effective_allow_rules(rules, PermissionMode.AUTO, all_shell) if r.behavior == "allow"] == ["Edit"]
    # in the pipeline: the stripped python rule no longer bypasses the judge
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.permissions.rules = [rule("Bash(python:*)")]
    ctx.classifier = FakeClassifier(ClassifierVerdict(True, 2, reason="runs code"))
    assert decide(BASH, {"command": "python evil.py"}, call_ctx(ctx)).behavior == "deny"
    assert ctx.classifier.calls == ["Bash"]


def test_auto_mode_denial_limit_falls_back_to_prompt(ctx, run):
    ctx.permissions.mode = PermissionMode.AUTO
    ctx.classifier = FakeClassifier(*[ClassifierVerdict(True, 2, reason="no")] * 3)
    assert run(BASH, {"command": "make a"}).behavior == "deny"
    assert run(BASH, {"command": "make b"}).behavior == "deny"
    assert run(BASH, {"command": "make c"}).behavior == "allow"  # third denial -> human said yes
    assert len(ctx.prompter.requests) == 1


# ---------------------------------------------------------------- mode transitions (R1-R4)


def test_repo_controlled_bypass_is_ignored():
    for src in (SettingsSource.PROJECT, SettingsSource.LOCAL):
        s = Settings.from_layers({src: {"permissions": {"defaultMode": "bypassPermissions"}}})
        assert modes.initial_mode(s, cli_mode=None, dangerously_skip=False) == PermissionMode.DEFAULT
    s = Settings.from_layers({SettingsSource.USER: {"permissions": {"defaultMode": "acceptEdits"}},
                              SettingsSource.PROJECT: {"permissions": {"defaultMode": "bypassPermissions"}}})
    assert modes.initial_mode(s, cli_mode=None, dangerously_skip=False) == PermissionMode.ACCEPT_EDITS
    s = Settings.from_layers({SettingsSource.USER: {"permissions": {"defaultMode": "bypassPermissions"}}})
    assert modes.initial_mode(s, cli_mode=None, dangerously_skip=False) == PermissionMode.BYPASS


def test_policy_disables_modes():
    s = Settings.from_layers({SettingsSource.POLICY: {"permissions": {"disableBypassPermissionsMode": "disable", "disableAutoMode": "disable"}}})
    assert modes.initial_mode(s, cli_mode=None, dangerously_skip=True) == PermissionMode.DEFAULT
    assert modes.initial_mode(s, cli_mode=PermissionMode.AUTO, dangerously_skip=False) == PermissionMode.DEFAULT


def test_runtime_switch_errors_and_plan_restore(ctx):
    perm = ctx.permissions
    with pytest.raises(modes.ModeChangeError) as e:
        modes.set_mode(perm, PermissionMode.BYPASS, Settings())
    assert e.value.reason == "not_launched"
    perm.bypass_available = True
    with pytest.raises(modes.ModeChangeError) as e:
        modes.set_mode(perm, PermissionMode.BYPASS, Settings(), is_cloud=True)
    assert e.value.reason == "cloud"
    with pytest.raises(modes.ModeChangeError) as e:
        modes.set_mode(perm, PermissionMode.BYPASS, Settings.from_dict({"permissions": {"disableBypassPermissionsMode": "disable"}}))
    assert e.value.reason == "disabled"
    with pytest.raises(modes.ModeChangeError) as e:
        modes.set_mode(perm, PermissionMode.BYPASS, Settings.from_dict({"permissions": {"restricted": True}}))
    assert e.value.reason == "restricted"
    modes.set_mode(perm, PermissionMode.ACCEPT_EDITS, Settings())
    modes.set_mode(perm, PermissionMode.PLAN, Settings())
    assert perm.pre_plan_mode == PermissionMode.ACCEPT_EDITS
    assert modes.exit_plan_mode(perm) == PermissionMode.ACCEPT_EDITS and perm.pre_plan_mode is None


# ---------------------------------------------------------------- sandbox interplay (R45)


def test_sandboxed_bash_auto_allowed_and_skips_whole_tool_ask(ctx, run):
    ctx.settings = Settings.from_dict({"sandbox": {"enabled": True, "allowUnsandboxedCommands": True}})
    ctx.permissions.rules = [rule("Bash", "ask")]
    assert run(BASH, {"command": "make build"}).behavior == "allow"
    assert not ctx.prompter.requests
    d = decide(BASH, {"command": "make build", "dangerouslyDisableSandbox": True}, call_ctx(ctx))
    assert d.behavior == "ask"


# ---------------------------------------------------------------- persistence (§2.5)


def test_apply_updates_persists_to_local_settings(ctx, project):
    apply_updates(ctx, [PermissionUpdate("addRules", "localSettings", [Rule("Bash", "make:*")], "allow")])
    data = json.loads((project / ".claude" / "settings.local.json").read_text())
    assert data["permissions"]["allow"] == ["Bash(make:*)"]
    assert Rule("Bash", "make:*", "allow", RuleSource.LOCAL) in ctx.permissions.rules
    apply_updates(ctx, [PermissionUpdate("removeRules", "localSettings", [Rule("Bash", "make:*")], "allow")])
    assert json.loads((project / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == []
    apply_updates(ctx, [PermissionUpdate("setMode", "session", mode=PermissionMode.BYPASS)])  # cannot unlock bypass
    assert ctx.permissions.mode == PermissionMode.DEFAULT
