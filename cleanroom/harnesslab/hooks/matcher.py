"""Hook matchers and per-event match fields (WP3).

Implements spec 04 R31 (empty / ``*`` / ``.*`` match all; identifier-and-``|``
patterns compared exactly incl. aliases - some events also allow ``,`` space
``-``; otherwise regex over name, aliases and MCP family names; invalid regex
never matches and is logged; the matched field depends on the event: tool
name, source, trigger, notification type, agent type, MCP server name, load
reason, file basename ...), 04 R32 (``if`` filter in permission-rule syntax via
``permissions.rules``) and 04 §4.7 ``matcher_matches``.

Design choices where the spec is silent: the exact per-event field table below
(``MATCH_FIELD``) is our own reading of "the matched field depends on the event";
events without an entry match every hook group regardless of its matcher.
Tool aliases come from ``payload["tool_aliases"]`` (the pipeline puts them there)
because the hook payload itself only carries the tool name.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from harnesslab.hooks.types import HookEvent

log = logging.getLogger(__name__)

#: event -> payload key whose value the matcher is compared against.
MATCH_FIELD: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "tool_name",
    HookEvent.POST_TOOL_USE: "tool_name",
    HookEvent.POST_TOOL_USE_FAILURE: "tool_name",
    HookEvent.PERMISSION_REQUEST: "tool_name",
    HookEvent.PERMISSION_DENIED: "tool_name",
    HookEvent.SESSION_START: "source",  # startup | resume | clear | compact
    HookEvent.SESSION_END: "reason",
    HookEvent.SETUP: "trigger",
    HookEvent.PRE_COMPACT: "trigger",  # manual | auto
    HookEvent.POST_COMPACT: "trigger",
    HookEvent.NOTIFICATION: "notification_type",
    HookEvent.SUBAGENT_START: "agent_type",
    HookEvent.SUBAGENT_STOP: "agent_type",
    HookEvent.ELICITATION: "mcp_server_name",
    HookEvent.ELICITATION_RESULT: "mcp_server_name",
    HookEvent.INSTRUCTIONS_LOADED: "load_reason",
    HookEvent.FILE_CHANGED: "file_path",  # compared by basename
    HookEvent.CONFIG_CHANGE: "source",
    HookEvent.PRE_MODEL_SWITCH: "model",
    HookEvent.POST_MODEL_SWITCH: "model",
    HookEvent.USER_PROMPT_EXPANSION: "command_name",
}

#: Events whose matcher field is a tool name: list patterns may use only ``|``.
_TOOL_EVENTS = {e for e, f in MATCH_FIELD.items() if f == "tool_name"}

_PLAIN_TOOL_LIST = re.compile(r"[A-Za-z0-9_|]+")
_PLAIN_OTHER_LIST = re.compile(r"[A-Za-z0-9_|, \-]+")


def event_match_values(event: HookEvent, payload: dict[str, Any]) -> list[str]:
    """Candidate strings (name + aliases + MCP family names) for the matcher."""
    key = MATCH_FIELD.get(event)
    if key is None:
        return []
    raw = payload.get(key)
    if raw is None:
        return []
    value = str(raw)
    if event == HookEvent.FILE_CHANGED:
        value = os.path.basename(value)
    values = [value]
    if key == "tool_name":
        values += [str(a) for a in payload.get("tool_aliases", []) or []]
        if value.startswith("mcp__"):
            server = value[len("mcp__") :].split("__", 1)[0]
            values += [f"mcp__{server}", f"mcp__{server}__*"]
        srv = payload.get("mcp_server")
        if isinstance(srv, dict) and srv.get("name"):
            values.append(f"mcp__{srv['name']}")
    out: list[str] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def matcher_matches(pattern: str | None, values: list[str], *, event: HookEvent | None = None) -> bool:
    """04 R31 / §4.7 ``matcher_matches``.

    * ``None``, ``""``, ``*`` and ``.*`` match everything (even with no value);
    * a plain list (``Edit|Write``; non-tool events may also use ``,``, spaces and
      ``-``) is compared *exactly* against each candidate value;
    * anything else is a regular expression searched in each value; an invalid
      regex is logged and never matches.
    """
    if pattern is None or pattern.strip() in ("", "*", ".*"):
        return True
    pat = pattern.strip()
    tool_like = event is None or event in _TOOL_EVENTS
    plain = _PLAIN_TOOL_LIST if tool_like else _PLAIN_OTHER_LIST
    if plain.fullmatch(pat):
        names = [n.strip() for n in re.split(r"[|]" if tool_like else r"[|,\s]+", pat) if n.strip()]
        return any(v in names for v in values)
    try:
        rx = re.compile(pat)
    except re.error as e:
        log.warning("invalid hook matcher %r: %s", pattern, e)
        return False
    return any(rx.search(v) for v in values)


def if_filter_matches(if_rule: str, payload: dict[str, Any], *, cwd: str) -> bool:
    """04 R32: the hook's ``if`` field is a permission rule (``Bash(git push:*)``,
    ``Edit(src/**)``) evaluated with the *same* matcher permission rules use.
    Payloads without a tool (non-tool events) never satisfy a tool rule; an
    unparseable ``if`` never matches (fail closed: the hook is skipped)."""
    from harnesslab.permissions.rules import RuleSyntaxError, parse_rule, rule_matches_call

    try:
        rule = parse_rule(if_rule, "ask")
    except RuleSyntaxError:
        log.warning("invalid hook if-filter %r", if_rule)
        return False
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return False
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return False
    return rule_matches_call(rule, tool_name, tool_input, cwd=cwd, aliases=payload.get("tool_aliases", []) or [])
