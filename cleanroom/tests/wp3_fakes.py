"""Shared fakes for WP3 tests (spec 04): minimal tools standing in for WP2's
built-ins, a call-context helper and a recording prompter."""

from __future__ import annotations

from typing import Any

from harnesslab.permissions.types import PermissionAnswer, PermissionPromptRequest, Rule, RuleSource
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext


class FakeBash(Tool):
    name = "Bash"
    kind = "execute"
    rule_content_field = "command"
    input_schema = {"type": "object", "properties": {"command": {"type": "string"}, "dangerouslyDisableSandbox": {"type": "boolean"}}}

    def is_read_only(self, input: dict[str, Any]) -> bool:
        from harnesslab.permissions.bash_safety import is_read_only

        return is_read_only(input.get("command", ""))


class FakeEdit(Tool):
    name = "Edit"
    kind = "edit"
    rule_content_field = "file_path"
    input_schema = {"type": "object", "properties": {"file_path": {}, "old_string": {}, "new_string": {}}}


class FakeWrite(FakeEdit):
    name = "Write"
    input_schema = {"type": "object", "properties": {"file_path": {}, "content": {}}}


class FakeRead(Tool):
    name = "Read"
    kind = "read"
    rule_content_field = "file_path"
    input_schema = {"type": "object", "properties": {"file_path": {}}}

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True


class FakeMcp(Tool):
    name = "mcp__github__create_issue"
    is_mcp = True
    input_schema = {"type": "object", "properties": {"title": {}}}


class FakeAgent(Tool):
    name = "Agent"
    aliases = ("Task",)
    input_schema = {"type": "object", "properties": {"subagent_type": {}, "prompt": {}}}


class InteractiveTool(Tool):
    name = "AskUser"
    input_schema = {"type": "object", "properties": {"question": {}}}

    def requires_user_interaction(self) -> bool:
        return True


class AllowingTool(Tool):
    """A tool whose own check says allow (to test R6 and hook interplay)."""

    name = "Thing"
    input_schema = {"type": "object", "properties": {"x": {}}}

    def check_permissions(self, input, call_ctx):  # type: ignore[override]
        from harnesslab.permissions.types import Decision, OtherReason

        return Decision.allow(OtherReason("tool says fine"))


def call_ctx(ctx: SessionContext, tool_use_id: str = "tu1") -> ToolCallContext:
    return ToolCallContext(session=ctx, tool_use_id=tool_use_id, abort=ctx.abort.signal)


def rule(text: str, behavior: str = "allow", source: RuleSource = RuleSource.USER) -> Rule:
    from harnesslab.permissions.rules import parse_rule

    return parse_rule(text, behavior, source)  # type: ignore[arg-type]


class RecordingPrompter:
    def __init__(self, behavior: str = "allow", **kw: Any) -> None:
        self.requests: list[PermissionPromptRequest] = []
        self.behavior = behavior
        self.kw = kw

    def __call__(self, req: PermissionPromptRequest) -> PermissionAnswer:
        self.requests.append(req)
        return PermissionAnswer(self.behavior, **self.kw)  # type: ignore[arg-type]
