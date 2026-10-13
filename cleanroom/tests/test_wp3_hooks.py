"""WP3 hook matcher/runner tests (spec 04 R31-R40, §2.6-§2.9, §4.7; 07 R35;
04#T9). Command hooks run tiny Python scripts via ``sys.executable``."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from harnesslab.agents.notifications import NotificationQueue
from harnesslab.config import Settings, SettingsSource
from harnesslab.hooks.matcher import event_match_values, if_filter_matches, matcher_matches
from harnesslab.hooks.runner import HookRunner, _interpolate_tool_input, common_payload, load_hook_config, merge, parse_hook_stdout
from harnesslab.hooks.types import HookEvent, HookExecResult, HookMatcherGroup, HookSpec
from harnesslab.model.base import APIError
from harnesslab.model.scripted import fail, say
from harnesslab.types import ErrorKind

PRE = HookEvent.PRE_TOOL_USE

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell hooks")


def script(tmp_path: Path, body: str, name: str = "hook.py") -> str:
    p = tmp_path / name
    p.write_text("import json, sys, time\n" + body)
    return f'"{sys.executable}" "{p}"'


def payload(ctx, event=PRE, **kw):
    kw.setdefault("tool_name", "Bash")
    kw.setdefault("tool_input", {"command": "git push origin main"})
    return common_payload(event, ctx, **kw)


def run(ctx, event, *specs, matcher=None, **kw):
    r = HookRunner({event: [HookMatcherGroup(matcher, list(specs))]})
    return r.run(event, payload(ctx, event, **kw), ctx)


# ---------------------------------------------------------------- matchers (R31, R32)


def test_matcher_forms():
    assert matcher_matches(None, ["Bash"]) and matcher_matches("", []) and matcher_matches("*", ["x"]) and matcher_matches(".*", ["x"])
    assert matcher_matches("Edit|Write", ["Write"], event=PRE)
    assert not matcher_matches("Edit|Write", ["Edit2"], event=PRE)  # exact, not substring
    assert matcher_matches("Ed.*", ["Edit"], event=PRE)  # regex
    assert matcher_matches("mcp__github", event_match_values(PRE, {"tool_name": "mcp__github__create_issue"}), event=PRE)
    assert not matcher_matches("(", ["("], event=PRE)  # invalid regex never matches
    assert matcher_matches("startup, resume", ["resume"], event=HookEvent.SESSION_START)
    assert matcher_matches("Task", event_match_values(PRE, {"tool_name": "Agent", "tool_aliases": ["Task"]}), event=PRE)
    assert event_match_values(HookEvent.FILE_CHANGED, {"file_path": "/a/b/.env"}) == [".env"]


def test_if_filter_uses_permission_rule_syntax(ctx):
    p = payload(ctx)
    assert if_filter_matches("Bash(git push:*)", p, cwd=str(ctx.cwd))
    assert not if_filter_matches("Bash(git status:*)", p, cwd=str(ctx.cwd))
    assert if_filter_matches("Bash(git push:*)", payload(ctx, tool_input={"command": "cd x && git push"}), cwd=str(ctx.cwd))
    assert if_filter_matches("Edit(src/**)", payload(ctx, tool_name="Edit", tool_input={"file_path": "src/a.py"}), cwd=str(ctx.cwd))
    assert not if_filter_matches("Bash(", p, cwd=str(ctx.cwd))


def test_runner_applies_matcher_and_if(ctx):
    seen: list[str] = []
    specs = [
        HookSpec("callback", callback=lambda p: seen.append("push") or None, if_="Bash(git push:*)"),
        HookSpec("callback", callback=lambda p: seen.append("status") or None, if_="Bash(git status:*)"),
    ]
    run(ctx, PRE, *specs, matcher="Bash")
    run(ctx, PRE, *specs, matcher="Edit")
    assert seen == ["push"]


# ---------------------------------------------------------------- execution & exit codes (R33)


def test_command_hook_gets_payload_on_stdin_and_json_output(ctx, tmp_path):
    cmd = script(tmp_path, """
data = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no pushes " + data["tool_input"]["command"]}, "systemMessage": data["hook_event_name"]}))
""")
    agg = run(ctx, PRE, HookSpec("command", command=cmd))
    assert agg.decision == "deny" and agg.decision_reason == "no pushes git push origin main"
    assert agg.system_messages == ["PreToolUse"] and agg.blocked


def test_exit_codes(ctx, tmp_path):
    two = script(tmp_path, "sys.stderr.write('stop that'); sys.exit(2)", "two.py")
    one = script(tmp_path, "sys.stderr.write('oops'); sys.exit(1)", "one.py")
    agg = run(ctx, PRE, HookSpec("command", command=two))
    assert agg.blocking_errors == ["stop that"] and agg.blocked
    agg = run(ctx, PRE, HookSpec("command", command=one))
    assert agg.user_notices == ["oops"] and not agg.blocked
    agg = run(ctx, PRE, HookSpec("command", command=one, on_failure="block"))
    assert agg.blocked
    # onFailure only matters for blocking-capable events
    agg = run(ctx, HookEvent.NOTIFICATION, HookSpec("command", command=one, on_failure="block"))
    assert not agg.blocked and agg.user_notices == ["oops"]
    agg = run(ctx, PRE, HookSpec("command", command="/nonexistent/hook-binary-xyz", args=[]))
    assert agg.user_notices and not agg.blocked  # spawn failure = exit 1 semantics


def test_plain_stdout_becomes_context_only_for_some_events(ctx, tmp_path):
    cmd = script(tmp_path, "print('remember the style guide')")
    agg = run(ctx, HookEvent.USER_PROMPT_SUBMIT, HookSpec("command", command=cmd))
    assert agg.additional_contexts == ["remember the style guide"]
    agg = run(ctx, PRE, HookSpec("command", command=cmd))
    assert agg.additional_contexts == [] and not agg.blocked
    agg = run(ctx, PRE, HookSpec("command", command=cmd, on_failure="block"))
    assert agg.blocked  # invalid JSON with onFailure: block


def test_exec_form_args_expand_only_known_vars(ctx, tmp_path):
    p = tmp_path / "argv.py"
    p.write_text("import sys; print(' '.join(sys.argv[1:]))")
    spec = HookSpec("command", command=sys.executable, args=[str(p), "${CLAUDE_PROJECT_DIR}", "$HOME"])
    agg = run(ctx, HookEvent.USER_PROMPT_SUBMIT, spec)
    assert agg.additional_contexts == [f"{ctx.cwd} $HOME"]


def test_env_file_for_session_start(ctx, tmp_path):
    cmd = script(tmp_path, "import os; print(os.environ.get('CLAUDE_ENV_FILE', 'none'))")
    agg = run(ctx, HookEvent.SESSION_START, HookSpec("command", command=cmd), source="startup")
    assert agg.additional_contexts[0].endswith("SessionStart.sh")
    assert ctx.extra["hook_env_files"]


# ---------------------------------------------------------------- timeouts (R36)


def test_timeout_discards_output_or_fails_closed(ctx, tmp_path):
    slow = script(tmp_path, "time.sleep(5); print(json.dumps({'decision': 'block'}))")
    t0 = time.monotonic()
    agg = run(ctx, PRE, HookSpec("command", command=slow, timeout=0.3))
    assert not agg.blocked and time.monotonic() - t0 < 4
    assert agg.results[0].outcome == "ignore"
    agg = run(ctx, PRE, HookSpec("command", command=slow, timeout=0.3, timeout_fails_closed=True))
    assert agg.blocked
    agg = run(ctx, PRE, HookSpec("callback", callback=lambda p: time.sleep(2), timeout=0.2, timeout_fails_closed=True))
    assert agg.blocked


def test_session_end_timeout_env(ctx):
    from harnesslab.hooks.runner import _timeout_for

    assert _timeout_for(HookSpec("command"), HookEvent.SESSION_END, {}) == 1.5
    assert _timeout_for(HookSpec("command"), HookEvent.SESSION_END, {"CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "999999"}) == 60
    assert _timeout_for(HookSpec("prompt"), PRE, {}) == 30


# ---------------------------------------------------------------- parallelism, dedupe, once (R34)


def test_hooks_run_in_parallel_on_same_input(ctx):
    barrier = threading.Barrier(2, timeout=3)
    seen = []

    def cb(p):
        barrier.wait()  # only passes if both run at the same time
        seen.append(dict(p["tool_input"]))
        p["tool_input"]["command"] = "mutated"
        return None

    run(ctx, PRE, HookSpec("callback", callback=cb), HookSpec("callback", callback=cb))
    assert seen == [{"command": "git push origin main"}] * 2


def test_dedupe_and_once(ctx, tmp_path):
    log = tmp_path / "log.txt"
    cmd = script(tmp_path, f"open({str(log)!r}, 'a').write('x')")
    run(ctx, PRE, HookSpec("command", command=cmd), HookSpec("command", command=cmd))
    assert log.read_text() == "x"
    runner = HookRunner({PRE: [HookMatcherGroup(None, [HookSpec("command", command=cmd, once=True)])]})
    runner.run(PRE, payload(ctx), ctx)
    runner.run(PRE, payload(ctx), ctx)
    assert log.read_text() == "xx"


# ---------------------------------------------------------------- merge (§4.7)


def test_merge_precedence():
    r = merge([
        HookExecResult(decision="allow", updated_input={"a": 1}, hook_name="h1"),
        HookExecResult(decision="ask", updated_input={"a": 2}, hook_name="h2"),
    ])
    assert r.decision == "ask" and r.updated_input == {"a": 2}
    r = merge([
        HookExecResult(decision="deny", decision_reason="first", hook_name="d1"),
        HookExecResult(decision="deny", decision_reason="second"),
        HookExecResult(decision="allow", updated_input={"x": 1}),
    ])
    assert r.decision == "deny" and r.decision_reason == "first" and r.hook_name == "d1" and r.updated_input is None
    r = merge([HookExecResult(classifier_context="a" * 1500), HookExecResult(classifier_context="b" * 1500)])
    assert len(r.classifier_context.encode("utf-16-le")) == 4000
    r = merge([HookExecResult(continue_=False, stop_reason="done"), HookExecResult(additional_context="ctx")])
    assert r.prevent_continuation and r.stop_reason == "done" and r.additional_contexts == ["ctx"]


def test_parse_hook_stdout_fields():
    r = parse_hook_stdout(PRE, json.dumps({"decision": "block", "reason": "nope"}))
    assert r.outcome == "block" and r.decision == "deny"
    r = parse_hook_stdout(HookEvent.POST_TOOL_USE, json.dumps({"hookSpecificOutput": {"additionalContext": "c", "classifierContext": "cc", "updatedMCPToolOutput": [1]}}))
    assert r.additional_context == "c" and r.classifier_context == "cc" and r.updated_tool_output == [1]
    r = parse_hook_stdout(HookEvent.PERMISSION_REQUEST, json.dumps({"hookSpecificOutput": {"decision": {"behavior": "deny", "message": "m"}}}))
    assert r.decision == "deny" and r.decision_reason == "m"
    r = parse_hook_stdout(HookEvent.SESSION_START, json.dumps({"hookSpecificOutput": {"sessionTitle": "T"}}))
    assert r.specific == {"sessionTitle": "T"}
    with pytest.raises(ValueError):
        parse_hook_stdout(PRE, "[1]")


def test_post_tool_use_classifier_context_reaches_ctx(ctx):
    cb = lambda p: {"hookSpecificOutput": {"classifierContext": "deploy approved by CI"}}  # noqa: E731
    run(ctx, HookEvent.POST_TOOL_USE, HookSpec("callback", callback=cb))
    assert ctx.extra["classifier_context"] == ["deploy approved by CI"]


# ---------------------------------------------------------------- settings (R39, §2.6)


def test_load_config_disable_all_and_managed_only(ctx, tmp_path):
    log = tmp_path / "log.txt"
    user_cmd = script(tmp_path, f"open({str(log)!r}, 'a').write('u')", "u.py")
    pol_cmd = script(tmp_path, f"open({str(log)!r}, 'a').write('p')", "p.py")
    layers = {
        SettingsSource.USER: {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": user_cmd, "timeout": 5}]}]}},
        SettingsSource.POLICY: {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": pol_cmd}]}]}},
    }
    cfg = load_hook_config(Settings.from_layers(layers))
    assert [g.matcher for g in cfg[PRE]] == ["Bash", None] and cfg[PRE][1].hooks[0].managed
    HookRunner(settings=Settings.from_layers(layers)).run(PRE, payload(ctx), ctx)
    assert sorted(log.read_text()) == ["p", "u"]
    log.write_text("")
    managed = {**layers, SettingsSource.FLAG: {"allowManagedHooksOnly": True}}
    HookRunner(settings=Settings.from_layers(managed)).run(PRE, payload(ctx), ctx)
    assert log.read_text() == "p"
    log.write_text("")
    off = {**layers, SettingsSource.FLAG: {"disableAllHooks": True}}
    assert not HookRunner(settings=Settings.from_layers(off)).run(PRE, payload(ctx), ctx).results
    assert log.read_text() == ""


# ---------------------------------------------------------------- other hook types


def test_callback_shapes(ctx):
    agg = run(ctx, PRE, HookSpec("callback", callback=lambda p: None))
    assert agg.decision is None and agg.results[0].outcome == "ok"
    agg = run(ctx, PRE, HookSpec("callback", callback=lambda p: HookExecResult("block", "cb says no")))
    assert agg.blocking_errors == ["cb says no"]
    runner = HookRunner()
    runner.register_callback(PRE, lambda p: {"hookSpecificOutput": {"permissionDecision": "ask"}}, matcher="Bash")
    assert runner.run(PRE, payload(ctx), ctx).decision == "ask"


def test_prompt_hook_verdicts_and_overflow_retry(ctx, scripted):
    scripted.add(say('{"ok": false, "reason": "tests were not run"}'))
    agg = run(ctx, HookEvent.STOP, HookSpec("prompt", prompt="Did the agent run tests? $ARGUMENTS"))
    assert agg.blocking_errors == ["tests were not run"]
    req = scripted.requests[-1]
    assert req.thinking == {"type": "disabled"} and req.query_source == "hook"
    scripted.add(say('{"ok": false, "reason": "cannot be done", "impossible": true}'))
    agg = run(ctx, HookEvent.STOP, HookSpec("prompt", prompt="Goal met?"))
    assert not agg.blocked and agg.results[0].specific == {"impossible": True}
    scripted.add(fail(APIError(ErrorKind.PROMPT_TOO_LONG)), say('{"ok": true, "reason": "fine"}'))
    agg = run(ctx, HookEvent.STOP, HookSpec("prompt", prompt="Goal met? $ARGUMENTS"), transcript="x" * 1000)
    assert not agg.blocked
    first, second = scripted.requests[-2:]
    assert len(second.messages[0]["content"][0]["text"]) < len(first.messages[0]["content"][0]["text"])


def test_http_hook_respects_allowlist(ctx):
    ctx.settings = Settings.from_dict({"allowedHttpHookUrls": ["https://hooks.internal/*"]})
    agg = run(ctx, PRE, HookSpec("http", url="https://evil.example/collect"))
    assert agg.user_notices and "not in allowedHttpHookUrls" in agg.user_notices[0]


def test_mcp_tool_hook_interpolates_input(ctx):
    class Client:
        name = "audit"

        def __init__(self):
            self.calls = []

        def call_tool(self, tool, args):
            self.calls.append((tool, args))
            return {"systemMessage": "logged"}

    c = Client()
    ctx.mcp_clients = [c]
    agg = run(ctx, PRE, HookSpec("mcp_tool", server="audit", tool="log", tool_input={"cmd": "${tool_input.command}"}))
    assert c.calls == [("log", {"cmd": "git push origin main"})] and agg.system_messages == ["logged"]
    assert _interpolate_tool_input(["${tool_input.missing}"], {}) == [""]


def test_async_rewake_notifies(ctx):
    ctx.notifications = NotificationQueue()
    agg = run(ctx, PRE, HookSpec("callback", callback=lambda p: HookExecResult("block", "lint failed"), async_rewake=True))
    assert not agg.blocked  # async hooks never block the call itself
    assert ctx.notifications.wait(3)
    (msg,) = ctx.notifications.drain()
    assert "lint failed" in msg.text()
