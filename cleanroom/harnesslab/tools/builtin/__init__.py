"""Built-in tools (WP2): Read, Write, Edit, Glob, Grep, Bash, TodoWrite, ToolSearch.

Implements the core of spec 03 §2.2's inventory. Agent / SendMessage / TaskStop
tools live in ``agents.agent_tool`` (WP5); Skill in ``extensions.skill_tool``
(WP5); MCP tools are wrapped by ``extensions.mcp`` (WP5). ``default_tools()``
returns the whole session pool, including the WP5 tools (they share one
launcher through ``ctx.extra["subagent_launcher"]``); ``core_tools()`` is the
WP2-only subset.

Deferred (out of scope for this iteration, WORKPLAN G16): NotebookEdit,
WebFetch / WebSearch (03 R61-R62), AskUserQuestion (R58), plan-mode tools
(R59), Monitor (R63), LSP (R64), PowerShell, task-list v2 tools.
"""

from __future__ import annotations

from harnesslab.tools.base import Tool


def default_tools() -> list[Tool]:
    """The default session tool pool: the core tools plus Agent, SendMessage,
    TaskStop (06 §2.2) and Skill (07 §2.3)."""
    from harnesslab.agents.agent_tool import AgentTool, SendMessageTool, TaskStopTool
    from harnesslab.extensions.skill_tool import SkillTool

    return [*core_tools(), AgentTool(), SendMessageTool(), TaskStopTool(), SkillTool()]


def core_tools() -> list[Tool]:
    """Fresh instances of every built-in tool in this package."""
    from harnesslab.tools.builtin.bash import BashTool
    from harnesslab.tools.builtin.edit import EditTool
    from harnesslab.tools.builtin.glob import GlobTool
    from harnesslab.tools.builtin.grep import GrepTool
    from harnesslab.tools.builtin.read import ReadTool
    from harnesslab.tools.builtin.todo import TodoWriteTool
    from harnesslab.tools.builtin.tool_search import ToolSearchTool
    from harnesslab.tools.builtin.write import WriteTool

    return [BashTool(), ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool(), TodoWriteTool(), ToolSearchTool()]
