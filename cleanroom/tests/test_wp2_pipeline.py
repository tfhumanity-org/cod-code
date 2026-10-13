"""WP2 per-call pipeline: 03 R1-R8, 04 R30, 03 R7."""

from __future__ import annotations

from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.permissions.types import Decision, HookReason, PermissionAnswer
from harnesslab.tools.base import Tool, ToolCallContext, ToolErrorCode, ToolOutput, ValidationResult
from harnesslab.tools.builtin.write import WriteTool
from harnesslab.tools.pipeline import denial_text, run_tool_use, tool_error
from harnesslab.types import ToolUseBlock

from tests.wp2_fakes import EchoTool, StubHooks, allow_all, block, call_ctx_for


def text_of(outcome) -> str:
    return outcome.result.tool_results()[0].text()


def test_unknown_tool(ctx):
    o = run_tool_use(block("Ghost", "t1"), None, call_ctx_for(ctx))
    assert o.error_code == ToolErrorCode.UNKNOWN_TOOL
    assert text_of(o).startswith("<tool_use_error>") and "Ghost" in text_of(o)


def test_malformed_json_echoes_first_bytes_and_causes(ctx):
    raw = '{"text": "C:\\Users\\me' + "x" * 400
    b = ToolUseBlock("t1", "Echo", {}, raw_input=raw, parse_error="Invalid \\escape")
    o = run_tool_use(b, EchoTool(), call_ctx_for(ctx))
    t = text_of(o)
    assert "InputValidationError" in t and "not valid JSON" in t
    assert "C:\\\\Users" in t  # echoed (repr-escaped) first bytes
    assert "more characters" in t  # echo is bounded
    assert "backslash" in t and "control character" in t and "cut off" in t
    assert o.result.tool_results()[0].is_error


def test_coercion_notes_reach_the_model(ctx, tmp_path):
    ctx.can_use_tool = allow_all
    target = tmp_path / "new.txt"
    o = run_tool_use(block("Write", "t1", file_path=str(target), file_text="hi"), WriteTool(), call_ctx_for(ctx))
    assert target.read_text() == "hi"
    assert "'file_text' was treated as 'content'" in text_of(o)


def test_schema_error_with_steer_and_toolsearch_hint(ctx):
    class Deferred(EchoTool):
        should_defer = True
        validation_error_steer = "Pass text as a string."

    tool = Deferred("Lazy")
    o = run_tool_use(block("Lazy", "t1", text=3), tool, call_ctx_for(ctx))
    t = text_of(o)
    assert "InputValidationError" in t and "expected string" in t
    assert "Pass text as a string." in t
    assert 'select:Lazy' in t
    assert o.error_code == ToolErrorCode.INPUT_VALIDATION

    ctx.discovered_tools.add("Lazy")
    o2 = run_tool_use(block("Lazy", "t2", text=3), tool, call_ctx_for(ctx))
    assert "select:Lazy" not in text_of(o2)


class Picky(EchoTool):
    def __init__(self, behavior="error"):
        super().__init__("Picky")
        self.behavior = behavior

    def validate_input(self, input, call_ctx):
        return ValidationResult.fail("risky input", ToolErrorCode.OUTSIDE_ALLOWED_DIRS, self.behavior)


def test_validate_input_error_records_code(ctx):
    ctx.can_use_tool = allow_all
    o = run_tool_use(block("Picky", "t1"), Picky(), call_ctx_for(ctx))
    assert o.error_code == ToolErrorCode.OUTSIDE_ALLOWED_DIRS
    assert text_of(o) == "<tool_use_error>risky input</tool_use_error>"


def test_validate_input_ask_becomes_a_prompt(ctx):
    ctx.can_use_tool = allow_all
    asked = []
    ctx.prompter = lambda req: (asked.append(req), PermissionAnswer("allow"))[1]
    tool = Picky("ask")
    o = run_tool_use(block("Picky", "t1", text="go"), tool, call_ctx_for(ctx))
    assert asked and asked[0].description == "risky input"
    assert text_of(o) == "go" and tool.calls

    ctx.prompter = lambda req: PermissionAnswer("deny", message="no thanks")
    o2 = run_tool_use(block("Picky", "t2"), Picky("ask"), call_ctx_for(ctx))
    assert o2.denied and "no thanks" in text_of(o2)

    ctx.prompter = None  # nobody to ask -> plain error
    o3 = run_tool_use(block("Picky", "t3"), Picky("ask"), call_ctx_for(ctx))
    assert o3.error_code == ToolErrorCode.OUTSIDE_ALLOWED_DIRS


def test_deny_path_uses_guidance_text(ctx):
    ctx.can_use_tool = lambda tool, inp, cc: Decision.deny("rule Echo denies")
    tool = EchoTool()
    o = run_tool_use(block("Echo", "t1"), tool, call_ctx_for(ctx))
    t = text_of(o)
    assert o.denied and o.error_code == ToolErrorCode.PERMISSION_DENIED
    assert "did NOT happen" in t and "do not report it as done" in t
    assert "works around" in t and "stop and explain" in t
    assert "rule Echo denies" in t
    assert tool.calls == []
    assert "non-interactive" in denial_text(tool, None, headless=True)


def test_updated_input_from_permissions_is_used(ctx):
    ctx.can_use_tool = lambda tool, inp, cc: Decision.allow(updated_input={"text": "rewritten"})
    o = run_tool_use(block("Echo", "t1", text="orig"), EchoTool(), call_ctx_for(ctx))
    assert text_of(o) == "rewritten"


def test_without_can_use_tool_the_tool_check_decides(ctx):
    class Guarded(EchoTool):
        def check_permissions(self, input, call_ctx):
            return Decision.deny("tool says no")

    o = run_tool_use(block("Echo", "t1"), Guarded(), call_ctx_for(ctx))
    assert o.denied
    o2 = run_tool_use(block("Echo", "t2", text="fine"), EchoTool(), call_ctx_for(ctx))
    assert text_of(o2) == "fine"


def test_empty_result_note(ctx):
    ctx.can_use_tool = allow_all
    o = run_tool_use(block("Echo", "t1", text=""), EchoTool("Quiet"), call_ctx_for(ctx))
    assert text_of(o) == "(Quiet completed with no output)"


def test_tool_exception_becomes_error_result(ctx):
    class Crash(EchoTool):
        def call(self, input, call_ctx):
            raise OSError("disk on fire")

    hooks = StubHooks()
    ctx.hook_runner = hooks
    o = run_tool_use(block("Crash", "t1"), Crash("Crash"), call_ctx_for(ctx))
    assert o.result.tool_results()[0].is_error and "disk on fire" in text_of(o)
    assert hooks.seen[0][0] == HookEvent.POST_TOOL_USE_FAILURE


def test_error_output_is_wrapped(ctx):
    class Fails(EchoTool):
        def call(self, input, call_ctx):
            return ToolOutput(data="bad thing", is_error=True)

    o = run_tool_use(block("F", "t1"), Fails("F"), call_ctx_for(ctx))
    assert text_of(o) == "<tool_use_error>bad thing</tool_use_error>"
    assert tool_error("x", text_of(o)).content == text_of(o)  # no double wrapping


def test_post_tool_use_hooks_add_context_and_replace_output(ctx):
    ctx.hook_runner = StubHooks({HookEvent.POST_TOOL_USE: AggregatedHookResult(additional_contexts=["lint: ok"])})
    o = run_tool_use(block("Echo", "t1", text="out"), EchoTool(), call_ctx_for(ctx))
    t = text_of(o)
    assert t.startswith("out") and "lint: ok" in t and "<system-reminder>" in t

    ctx.hook_runner = StubHooks(
        {HookEvent.POST_TOOL_USE: AggregatedHookResult(updated_tool_output="redacted", prevent_continuation=True)}
    )
    o2 = run_tool_use(block("Echo", "t2", text="secret"), EchoTool(), call_ctx_for(ctx))
    assert text_of(o2) == "redacted" and o2.end_turn == "hook_stopped"


def test_hook_defer_only_headless_and_alone(ctx):
    ctx.can_use_tool = lambda tool, inp, cc: Decision("allow", HookReason("h", "defer"))
    tool = EchoTool()
    ctx.permissions.headless = True
    b = block("Echo", "t1", text="x")
    o = run_tool_use(b, tool, call_ctx_for(ctx, siblings=[b]))
    assert o.end_turn == "tool_deferred" and tool.calls == []

    two = [b, block("Echo", "t2")]
    o2 = run_tool_use(b, tool, call_ctx_for(ctx, siblings=two))
    assert o2.end_turn is None and tool.calls  # not alone: decision stands

    ctx.permissions.headless = False
    o3 = run_tool_use(b, tool, call_ctx_for(ctx, siblings=[b]))
    assert o3.end_turn is None


def test_aborted_before_call_gives_interrupted(ctx):
    from harnesslab.util.abort import AbortController

    c = AbortController()
    c.abort()
    tool = EchoTool()
    cc = ToolCallContext(session=ctx, tool_use_id="t1", abort=c.signal)
    o = run_tool_use(block("Echo", "t1"), tool, cc)
    assert o.error_code == ToolErrorCode.INTERRUPTED and tool.calls == []


def test_result_message_shape(ctx):
    cc = call_ctx_for(ctx)
    cc.assistant_uuid = "asst-1"

    class Structured(Tool):
        name = "S"

        def call(self, input, call_ctx):
            return ToolOutput(data={"a": 1})

    o = run_tool_use(block("S", "t1"), Structured(), cc)
    m = o.result
    assert m.role == "user" and len(m.content) == 1
    assert m.source_tool_assistant_uuid == "asst-1"
    assert m.tool_use_result == {"a": 1}
    assert '"a": 1' in text_of(o)
