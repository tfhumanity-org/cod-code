"""TodoWrite tool (WP2).

Implements spec 03 R57: replaces the whole list each call, keyed per agent
(``ctx.todos[agent_id or "main"]``); when every item is completed the list is
cleared; the result nudges continued use. Resets
``ctx.counters.turns_since_todo_write`` (feeds the 02 R32 todo reminder).
"""

from __future__ import annotations

from typing import Any

from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput

TODO_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
        "activeForm": {"type": "string", "minLength": 1},
    },
    "required": ["content", "status", "activeForm"],
}


class TodoWriteTool(Tool):
    name = "TodoWrite"
    input_schema = {
        "type": "object",
        "properties": {"todos": {"type": "array", "items": TODO_ITEM_SCHEMA}},
        "required": ["todos"],
        "additionalProperties": False,
    }
    should_defer = True
    search_hint = "track a task list"

    def description(self, ctx: Any = None) -> str:
        return "Create and update a structured task list for the current work."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        text = (
            "Maintains your task list for multi-step work. Each call replaces the whole list. Items have "
            "content (imperative, e.g. 'Run tests'), activeForm (progressive, e.g. 'Running tests') and "
            "status pending/in_progress/completed. Keep exactly one item in_progress, and mark items "
            "completed as soon as they are done. Skip it for trivial single-step requests."
        )
        return text if lean else text + "\n\nUse it when a request has three or more distinct steps or the user lists several tasks."

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        key = ctx.agent_id or "main"
        old = ctx.todos.get(key, [])
        new = [dict(t) for t in input["todos"]]
        all_done = bool(new) and all(t["status"] == "completed" for t in new)
        ctx.todos[key] = [] if all_done else new
        ctx.counters.turns_since_todo_write = 0
        text = (
            "Todo list updated. Keep using it to track progress: move the next item to in_progress and "
            "mark each item completed as soon as it is done."
        )
        if all_done:
            text = "All items are completed, so the todo list has been cleared."
        return ToolOutput(data=text)
