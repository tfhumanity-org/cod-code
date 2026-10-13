"""WP3 shell analysis tests (spec 04 R12-R17, §4.3; 03 R43)."""

from __future__ import annotations

import pytest

from harnesslab.permissions.bash_safety import (
    MAX_ANALYSIS_LEN,
    Redirect,
    check_bash,
    is_read_only,
    parse_shell,
    suggest_prefix,
)
from harnesslab.permissions.hazards import breaker, is_dangerous_removal_target, is_sensitive_path
from harnesslab.permissions.types import PermissionContext, Rule, RuleReason, SafetyCheckReason, SubcommandResultsReason

CWD = "/work/proj"
HOME = "/home/me"


def check(cmd: str, rules: list[Rule] | None = None):
    return check_bash(cmd, PermissionContext(rules=rules or []), cwd=CWD, home=HOME)


# ---------------------------------------------------------------- parser (R12)


def test_parser_splits_operators_and_redirects():
    ast = parse_shell("FOO=1 git log --oneline 2>/dev/null | head -5 && echo '>' done &")
    assert ast.kind == "ok"
    assert [c.argv for c in ast.commands] == [["git", "log", "--oneline"], ["head", "-5"], ["echo", ">", "done"]]
    assert ast.operators == ["|", "&&", "&"]
    assert ast.commands[0].env == {"FOO": "1"}
    assert ast.commands[0].redirects == [Redirect(">", "/dev/null", "2")]
    assert ast.commands[2].background


@pytest.mark.parametrize(
    "cmd,reason",
    [
        ("cat <<EOF\nhi\nEOF", "here-document"),
        ("echo 'unterminated", "unterminated quote"),
        ("if true; then ls; fi", "control structure"),
        ("echo $((1+2))", "arithmetic"),
        ("ls &&", "missing"),
    ],
)
def test_parser_too_complex_fails_safe(cmd, reason):
    ast = parse_shell(cmd)
    assert ast.kind == "too-complex" and reason in ast.reason
    assert not is_read_only(cmd)


def test_parser_flags_substitutions_and_subshells():
    assert parse_shell("echo $(date)").has_command_substitution
    assert parse_shell('echo "`date`"').has_command_substitution
    assert parse_shell("diff <(ls a) <(ls b)").has_process_substitution
    assert parse_shell("(cd x; ls)").has_subshell
    assert parse_shell("echo $HOME").has_unquoted_expansion
    assert not parse_shell("echo '$HOME'").has_unquoted_expansion


def test_redirect_safety():
    assert Redirect("<", "in.txt").is_safe()
    assert Redirect("<<<", "word").is_safe()
    assert Redirect(">", "/dev/null", "2").is_safe()
    assert Redirect(">&", "1", "2").is_safe()
    assert not Redirect(">", "out.txt").is_safe()
    assert not Redirect(">>", "/etc/passwd").is_safe()


# ---------------------------------------------------------------- read-only (R13, §4.3)


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la",
        "cat a.txt | head -5",
        "git status",
        "git log --oneline -5",
        "git -C sub log -n 3",
        "git diff --stat HEAD~1",
        "grep -rn foo src",
        "find . -name '*.py'",
        "jq .name package.json",
        "echo hi 2>/dev/null",
        "xargs cat",
        "sed -n 's/a/b/p' file",
        "LANG=C sort -u file",
        "wc -l < input.txt",
    ],
)
def test_read_only_allowlist(cmd):
    assert is_read_only(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c core.pager=evil log",
        "git --exec-path=/tmp log",
        "git push",
        "git branch -D main",
        "git branch newbranch",
        "git log --output=x",
        "ls > out.txt",
        "echo $(rm -rf x)",
        "echo $HOME",
        "(ls)",
        "cat x &",
        "FOO=1 ls",
        "find . -delete",
        "find . -exec rm {} ;",
        "jq -f prog.jq x",
        "sed -i s/a/b/ file",
        "sed 's/a/b/w out' file",
        "sort -o out file",
        "rm file",
        "cd sub && git log",
        "cat /dev/tcp/1.2.3.4/80",
        "xargs rm",
    ],
)
def test_not_read_only(cmd):
    assert not is_read_only(cmd), cmd


def test_overlong_commands_skip_analysis():
    assert not is_read_only("ls " + "a" * MAX_ANALYSIS_LEN)


# ---------------------------------------------------------------- compound checks (R14)


def test_compound_combines_by_severity():
    d = check("git status && git log")
    assert d.behavior == "allow" and isinstance(d.reason, SubcommandResultsReason)
    d = check("git status && make build")
    assert d.behavior == "passthrough"
    d = check("git status && rm -rf build", [Rule("Bash", "rm:*", "deny")])
    assert d.behavior == "deny" and isinstance(d.reason, RuleReason)


def test_allow_rules_must_cover_every_subcommand():
    rules = [Rule("Bash", "make:*", "allow"), Rule("Bash", "npm test", "allow")]
    assert check("make build && npm test", rules).behavior == "allow"
    assert check("make build && npm publish", rules).behavior == "passthrough"


def test_ask_rule_beats_read_only():
    d = check("git log", [Rule("Bash", "git log:*", "ask")])
    assert d.behavior == "ask" and d.matched_ask_rule == Rule("Bash", "git log:*", "ask")


@pytest.mark.parametrize("cmd", ["cd a && cd b && ls", "cd sub && git status", "mkdir -p .git/hooks && git status"])
def test_forced_asks(cmd):
    assert check(cmd).behavior == "ask"


# ---------------------------------------------------------------- dangerous removal (R16)


@pytest.mark.parametrize(
    "target,dangerous",
    [
        ("/", True),
        ("/usr", True),
        ("/usr/*", True),
        ("~", True),
        ("$HOME", True),
        ("$UNSET/", True),
        ("${DIR}/build", True),
        (".", True),
        ("..", True),
        ("*", True),
        (CWD, True),
        ("build", False),
        ("./dist/", False),
        ("/tmp/scratch", False),
    ],
)
def test_dangerous_removal_targets(target, dangerous):
    assert is_dangerous_removal_target(target, cwd=CWD, home=HOME) is dangerous


def test_dangerous_removal_is_bypass_immune_safety_check():
    d = check("rm -rf $UNSET/")
    assert d.behavior == "ask"
    assert isinstance(d.reason, SafetyCheckReason) and d.reason.circuit_breaker == "dangerousRemoval"
    assert breaker("dangerousRemoval").bypass_immune
    assert check("rm -rf build").behavior == "passthrough"


# ---------------------------------------------------------------- sed / substitution / background (R17)


def test_sed_write_and_exec_ask():
    assert check("sed 's/a/b/w out.txt' f").behavior == "ask"
    assert check("sed -e '1e date' f").behavior == "ask"
    assert check("sed -n 's/a/b/p' f").behavior == "allow"


def test_process_substitution_asks_and_background_is_safety_check():
    assert check("diff <(ls a) <(ls b)").behavior == "ask"
    d = check("sleep 5 &")
    assert isinstance(d.reason, SafetyCheckReason) and d.reason.circuit_breaker == "backgroundOperator"


def test_redirect_outside_working_dir_asks():
    d = check("echo hi > /etc/motd")
    assert d.behavior == "ask" and d.blocked_path == "/etc/motd"
    assert check("echo hi > out.txt").behavior == "passthrough"


# ---------------------------------------------------------------- suggestions (R15)


def test_prefix_suggestions():
    assert suggest_prefix(["git", "commit", "-m", "x"]) == ["git", "commit"]
    assert suggest_prefix(["npm", "run", "build", "--", "--watch"]) == ["npm", "run", "build"]
    assert suggest_prefix(["sudo", "npm", "test"]) == ["sudo", "npm", "test"]
    assert suggest_prefix(["FOO=1", "make", "all"]) == ["make", "all"]
    assert suggest_prefix(["weird", "thing"]) == ["weird"]
    # depth is bounded: sudo -> env -> sudo -> ... stops
    assert len(suggest_prefix(["sudo", "env", "sudo", "env", "sudo", "ls"])) <= 4


def test_passthrough_carries_rule_suggestion():
    d = check("make build")
    assert d.suggestions and d.suggestions[0].rules == [Rule("Bash", "make build:*", "allow")]


# ---------------------------------------------------------------- sensitive paths (R18)


@pytest.mark.parametrize(
    "path,sensitive",
    [
        (".git/config", True),
        (".vscode/settings.json", True),
        ("sub/.bashrc", True),
        ("~/.gitconfig", True),
        (".config/git/config", True),
        (".claude/settings.json", True),
        (".claude/skills/x/SKILL.md", False),
        (".claude/agents/a.md", False),
        ("src/main.py", False),
    ],
)
def test_sensitive_paths(path, sensitive):
    assert is_sensitive_path(path, cwd=CWD) is sensitive
