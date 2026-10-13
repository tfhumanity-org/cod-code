"""Per-call tool pipeline: parse -> coerce -> validate -> permissions -> call -> finalize (WP2).

Implements spec 03 R1-R8 and the R6 order:

    unknown tool?            -> error result (03 R1, 01 R11)
    JSON parse error?        -> InputValidationError echoing the first bytes + likely
                                causes (unescaped backslashes, control chars, truncation) (03 R2)
    coerce_input             -> notes telling the model what was rewritten (03 R3)
    schema validation        -> InputValidationError + steer + ToolSearch hint if the
                                tool is deferred and its schema was never sent (03 R4)
    validate_input           -> error with errorCode, or "ask" (03 R5)
    permissions              -> ctx.can_use_tool(tool, input, call_ctx): PreToolUse hooks,
                                rules, mode, classifier, prompt (04 §4.1) -> allow/deny
                                (hook "defer" honoured only headless + alone in batch, 03 R7)
    call                     -> ToolOutput
    map_result               -> ToolResultBlock
    empty-result substitution-> "(<Tool> completed with no output)" (03 R8)
    size budget / spill      -> tools.output.finalize_result (03 R23-R24)
    PostToolUse hooks        -> may add context / replace output (04 §2.9)

Errors are wrapped as ``<tool_use_error>...</tool_use_error>`` with
``is_error=True`` (03 §2.4). Denials use the 04 R30 guidance text.

Design choices (spec silent / seams)
------------------------------------
* PreToolUse hooks run *inside* ``ctx.can_use_tool`` (WP3, 04 §4.1), not here;
  their ``additionalContext`` (left in ``ctx.extra["pre_tool_hook_context"]``)
  is appended to the result as a reminder.
* The 04 R30 guidance is added once: ``can_use_tool`` already appends it to
  its denials, so ``denial_text`` only adds its own copy when it is missing.
* No ``can_use_tool`` wired (unit tests, bare embeddings): the tool's own
  ``check_permissions`` decides; ``deny`` denies, anything else allows.
* Hook "defer" (R7) arrives as a ``Decision`` whose reason is
  ``HookReason(reason="defer")``. It is honoured (``end_turn="tool_deferred"``)
  only when the session is headless and the call is alone in its batch;
  otherwise the decision's own behaviour stands.
* ``validate_input`` returning ``behavior="ask"`` asks ``ctx.prompter``
  directly (the message is the question); with no prompter the validation
  message is returned as an error. An approval still goes through
  ``can_use_tool`` so deny rules keep priority.
* PostToolUse ``additional_contexts`` and blocking feedback are appended to the
  result text inside a ``<system-reminder>`` (keeps tool_use/tool_result
  pairing simple); ``updated_tool_output`` replaces the result text.

Deferred
--------
* 03 §2.1 "execution location changed between permission check and call"
  refusal and multi-machine ``_host`` routing (no remote machines here).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from harnesslab.hooks.types import HookEvent
from harnesslab.permissions.types import Decision, HookReason, PermissionPromptRequest
from harnesslab.tools.base import ContextLayer, Tool, ToolCallContext, ToolErrorCode, ToolOutput
from harnesslab.tools.output import finalize_result
from harnesslab.tools.registry import is_deferred_tool, remote_deferred_for
from harnesslab.types import (
    SYNTHETIC_RESULT_TEXT,
    ConversationItem,
    Message,
    SyntheticReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from harnesslab.util.abort import AbortedError
from harnesslab.util.schema import validate
from harnesslab.util.text import wrap_system_reminder

FIRST_BYTES_ECHO = 200  # design choice: how much malformed input to echo back (03 R2)
ERROR_OPEN, ERROR_CLOSE = "<tool_use_error>", "</tool_use_error>"


@dataclass
class ToolCallOutcome:
    """Everything one call produced."""

    result: Message  # user-role message holding exactly one ToolResultBlock
    error_code: ToolErrorCode = ToolErrorCode.NONE
    context_layers: list[ContextLayer] = field(default_factory=list)
    new_messages: list[ConversationItem] = field(default_factory=list)
    denied: bool = False
    end_turn: str | None = None


# ---------------------------------------------------------------- envelopes


def tool_error(tool_use_id: str, text: str) -> ToolResultBlock:
    """``<tool_use_error>`` envelope (03 §2.4)."""
    if text.startswith(ERROR_OPEN):
        return ToolResultBlock(tool_use_id, text, True)
    return ToolResultBlock(tool_use_id, f"{ERROR_OPEN}{text}{ERROR_CLOSE}", True)


def result_message(block: ToolResultBlock, *, assistant_uuid: str | None, tool_use_result: object = None) -> Message:
    """User row carrying one tool result; ``source_tool_assistant_uuid`` set (05 R36)."""
    return Message(
        "user",
        [block],
        source_tool_assistant_uuid=assistant_uuid,
        tool_use_result=tool_use_result,
    )


def _error_outcome(block: ToolUseBlock, call_ctx: ToolCallContext, text: str, code: ToolErrorCode, **kw) -> ToolCallOutcome:
    msg = result_message(tool_error(block.id, text), assistant_uuid=call_ctx.assistant_uuid)
    return ToolCallOutcome(msg, error_code=code, **kw)


def synthetic_outcome(block: ToolUseBlock, reason: SyntheticReason, assistant_uuid: str | None) -> ToolCallOutcome:
    """A result that explains why there is no real result (01 R52, 03 R13)."""
    code = ToolErrorCode.INTERRUPTED if reason is SyntheticReason.INTERRUPTED else ToolErrorCode.NONE
    msg = result_message(tool_error(block.id, SYNTHETIC_RESULT_TEXT[reason]), assistant_uuid=assistant_uuid)
    return ToolCallOutcome(msg, error_code=code)


# ----------------------------------------------------------------- texts


def format_parse_error(block: ToolUseBlock) -> str:
    """R2: echo the first bytes received and list the likely causes."""
    raw = block.raw_input or ""
    echo = raw[:FIRST_BYTES_ECHO]
    more = f" ... ({len(raw) - FIRST_BYTES_ECHO} more characters)" if len(raw) > FIRST_BYTES_ECHO else ""
    return (
        f"InputValidationError: the input for {block.name} is not valid JSON"
        f"{f' ({block.parse_error})' if block.parse_error else ''}.\n"
        f"Received: {echo!r}{more}\n"
        "Likely causes: a backslash that was not escaped (write \\\\ inside JSON strings), a raw "
        "control character such as a newline or tab inside a string (use \\n / \\t), or the input "
        "was cut off before the closing brace. Fix the JSON and call the tool again."
    )


def format_schema_errors(tool: Tool, errors: list[str], *, deferred_unsent: bool) -> str:
    """R4: list the schema errors, add the tool's steer and, for a deferred tool
    whose schema the model never received, how to load it."""
    lines = [f"InputValidationError: {tool.name} was called with invalid input:"]
    lines += [f"- {e}" for e in errors]
    if tool.validation_error_steer:
        lines.append(tool.validation_error_steer)
    if deferred_unsent:
        lines.append(
            f"The schema for {tool.name} has not been loaded in this conversation, so its parameters "
            f'were guessed. Call ToolSearch with query "select:{tool.name}" to load it, then retry.'
        )
    return "\n".join(lines)


LOCAL_DENIAL_GUIDANCE = (
    "This action did NOT happen, so do not report it as done. You may continue with other "
    "legitimate tools, but do not try to achieve the same effect in a way that works around "
    "this denial (for example through a different tool or command). If this action is "
    "essential to the task, stop and explain to the user what you need and why."
)


def _has_guidance(message: str) -> bool:
    """True when the permission layer already appended its 04 R30 guidance.

    Integration note: ``permissions.pipeline.make_can_use_tool`` appends
    ``DENIAL_GUIDANCE`` to every deny it returns, so this module adds its own
    copy only for denials that did not come through it (validator asks, bare
    ``check_permissions`` fallback, test lambdas) - one guidance per result."""
    try:
        from harnesslab.permissions.pipeline import DENIAL_GUIDANCE
    except Exception:  # pragma: no cover
        return False
    return DENIAL_GUIDANCE in message


def denial_text(tool: Tool, message: str | None, *, headless: bool) -> str:
    """04 R30: did not happen; do not claim success; do not circumvent; stop and explain if essential."""
    msg = (message or "").strip()
    why = f" Reason: {msg}" if msg else ""
    where = (
        "no one was available to approve it in this non-interactive session"
        if headless
        else "permission to use it was denied"
    )
    head = f"The {tool.name} call was not run because {where}.{why}"
    if msg and _has_guidance(msg):
        return head
    return f"{head}\n{LOCAL_DENIAL_GUIDANCE}"


# ------------------------------------------------------------- the pipeline


def _is_headless(call_ctx: ToolCallContext) -> bool:
    s = call_ctx.session
    return bool(s.permissions.headless or not s.is_interactive)


def _decide(tool: Tool, inp: dict, call_ctx: ToolCallContext) -> Decision:
    can = call_ctx.session.can_use_tool
    if can is not None:
        return can(tool, inp, call_ctx)
    d = tool.check_permissions(inp, call_ctx)
    if d.behavior == "deny":
        return d
    return Decision.allow(d.reason, d.updated_input)


def _ask_from_validator(tool: Tool, inp: dict, call_ctx: ToolCallContext, message: str) -> Decision | None:
    """R5 "ask": turn a validation failure into a question for the human."""
    prompter = call_ctx.session.prompter
    if prompter is None:
        return None
    req = PermissionPromptRequest(
        tool_name=tool.name,
        input=inp,
        tool_use_id=call_ctx.tool_use_id,
        decision=Decision.ask(message=message),
        agent_id=call_ctx.session.agent_id,
        decision_reason_text=message,
        title=f"{tool.name} needs confirmation",
        description=message,
    )
    answer = prompter(req)
    if answer.behavior == "allow":
        return Decision.allow(updated_input=answer.updated_input)
    return Decision.deny(answer.message or "The user declined.")


def _append_text(block: ToolResultBlock, extra: str) -> ToolResultBlock:
    if isinstance(block.content, str):
        sep = "\n\n" if block.content else ""
        return ToolResultBlock(block.tool_use_id, block.content + sep + extra, block.is_error)
    return ToolResultBlock(block.tool_use_id, [*block.content, TextBlock(extra)], block.is_error)


def _post_hooks(tool: Tool, inp: dict, block: ToolResultBlock, out: ToolOutput, call_ctx: ToolCallContext) -> tuple[ToolResultBlock, bool]:
    """PostToolUse hooks (R6 last step). Returns ``(block, prevent_continuation)``."""
    runner = call_ctx.session.hook_runner
    if runner is None:
        return block, False
    payload = {
        "tool_name": tool.name,
        "tool_input": inp,
        "tool_response": out.data if out.data is not None else block.text(),
        "tool_use_id": call_ctx.tool_use_id,
    }
    agg = runner.run(HookEvent.POST_TOOL_USE, payload, call_ctx.session)
    if agg.updated_tool_output is not None:
        new = agg.updated_tool_output
        text = new if isinstance(new, str) else json.dumps(new, default=str)
        block = ToolResultBlock(block.tool_use_id, text, block.is_error)
    notes = [*agg.blocking_errors, *agg.additional_contexts]
    if notes:
        block = _append_text(block, wrap_system_reminder("PostToolUse hook feedback:\n" + "\n".join(notes)))
    return block, agg.prevent_continuation


def run_tool_use(block: ToolUseBlock, tool: Tool | None, call_ctx: ToolCallContext) -> ToolCallOutcome:
    """Run one tool call through the full R6 pipeline. Never raises."""
    ctx = call_ctx.session

    # R1 / 01 R11 - unknown tool
    if tool is None:
        return _error_outcome(
            block,
            call_ctx,
            f"No such tool available: {block.name}. Only the tools listed in this conversation can be called.",
            ToolErrorCode.UNKNOWN_TOOL,
        )

    # R2 - malformed JSON
    if block.parse_error is not None:
        return _error_outcome(block, call_ctx, format_parse_error(block), ToolErrorCode.INPUT_VALIDATION)

    # R3 - coercion, then R4 - schema
    try:
        inp, coercion_notes = tool.coerce_input(dict(block.input))
    except Exception as e:  # a buggy coercer must not take the turn down
        inp, coercion_notes = dict(block.input), [f"(input coercion failed: {e})"]
    errors = validate(inp, tool.input_schema)
    if errors:
        unsent = is_deferred_tool(tool, remote_deferred=remote_deferred_for(ctx)) and tool.name not in ctx.discovered_tools
        return _error_outcome(
            block, call_ctx, format_schema_errors(tool, errors, deferred_unsent=unsent), ToolErrorCode.INPUT_VALIDATION
        )

    # R5 - tool-specific validation
    pre_approved: Decision | None = None
    try:
        v = tool.validate_input(inp, call_ctx)
    except Exception as e:
        return _error_outcome(block, call_ctx, f"Validation failed: {e}", ToolErrorCode.INPUT_VALIDATION)
    if not v.ok:
        if v.behavior == "ask":
            pre_approved = _ask_from_validator(tool, inp, call_ctx, v.message)
        if pre_approved is None:
            return _error_outcome(block, call_ctx, v.message, v.error_code)
        if pre_approved.behavior == "deny":
            text = denial_text(tool, pre_approved.message, headless=False)
            return _error_outcome(block, call_ctx, text, ToolErrorCode.PERMISSION_DENIED, denied=True)

    # R6 - permissions (PreToolUse hooks run inside can_use_tool)
    if call_ctx.abort.aborted:
        return synthetic_outcome(block, SyntheticReason.INTERRUPTED, call_ctx.assistant_uuid)
    try:
        decision = _decide(tool, inp, call_ctx)
    except AbortedError:
        return synthetic_outcome(block, SyntheticReason.INTERRUPTED, call_ctx.assistant_uuid)
    call_ctx.permission_decision = decision
    if isinstance(decision.reason, HookReason) and decision.reason.reason == "defer":
        if _is_headless(call_ctx) and len(call_ctx.siblings) <= 1:
            msg = result_message(
                ToolResultBlock(block.id, f"The {tool.name} call was deferred to the host by a hook; it has not run yet."),
                assistant_uuid=call_ctx.assistant_uuid,
            )
            return ToolCallOutcome(msg, end_turn="tool_deferred")
    if decision.behavior == "deny":
        text = denial_text(tool, decision.message, headless=_is_headless(call_ctx))
        return _error_outcome(block, call_ctx, text, ToolErrorCode.PERMISSION_DENIED, denied=True)
    if decision.updated_input is not None:
        inp = decision.updated_input

    # call
    try:
        call_ctx.abort.raise_if_aborted()
        out = tool.call(inp, call_ctx)
    except AbortedError:
        return synthetic_outcome(block, SyntheticReason.INTERRUPTED, call_ctx.assistant_uuid)
    except Exception as e:
        if ctx.hook_runner is not None:
            try:
                ctx.hook_runner.run(
                    HookEvent.POST_TOOL_USE_FAILURE,
                    {"tool_name": tool.name, "tool_input": inp, "tool_use_id": block.id, "error": str(e)},
                    ctx,
                )
            except Exception:
                pass
        return _error_outcome(block, call_ctx, f"{tool.name} failed: {type(e).__name__}: {e}", ToolErrorCode.NONE)

    # map_result -> empty substitution + spill -> PostToolUse
    if coercion_notes:
        out.notes = [*coercion_notes, *out.notes]
    rb = tool.map_result(out, block.id)
    if rb.is_error and isinstance(rb.content, str):
        rb = tool_error(block.id, rb.content)
    rb = finalize_result(tool, rb, ctx)
    pre_ctx = (ctx.extra.get("pre_tool_hook_context") or {}).pop(block.id, None)
    if pre_ctx:  # PreToolUse additionalContext left by permissions.pipeline (04 §4.1 step 0)
        rb = _append_text(rb, wrap_system_reminder("PreToolUse hook context:\n" + "\n".join(pre_ctx)))
    prevent = False
    try:
        rb, prevent = _post_hooks(tool, inp, rb, out, call_ctx)
    except Exception:
        pass
    end_turn = out.end_turn or ("hook_stopped" if prevent else None)
    return ToolCallOutcome(
        result_message(rb, assistant_uuid=call_ctx.assistant_uuid, tool_use_result=out.data),
        context_layers=list(out.context_layers),
        new_messages=list(out.new_messages),
        end_turn=end_turn,
    )
