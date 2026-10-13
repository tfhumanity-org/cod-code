"""Hook vocabulary: events, specs, per-hook and merged results (FINAL - architect).

Spec coverage
-------------
* 04 §2.6  hook configuration shape (``HookSpec``, ``HookMatcherGroup``).
* 04 §2.7  the 33 hook events (``HookEvent``).
* 04 §2.9  stdout JSON fields -> ``HookExecResult`` fields.
* 04 R33   exit code semantics -> ``HookOutcome``.
* 04 R36   default timeouts per hook type (``DEFAULT_TIMEOUT_S``).
* 04 §4.7  ``merge`` result shape -> ``AggregatedHookResult``.

Design choice (spec silent for an in-process library): in addition to the
spec's ``command`` / ``prompt`` / ``agent`` / ``http`` / ``mcp_tool`` types we add
``callback`` - a Python callable - which is how an embedding host (07 R35
``hook_callback``) and tests register hooks without spawning processes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


class HookEvent(str, enum.Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    POST_TOOL_BATCH = "PostToolBatch"
    NOTIFICATION = "Notification"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    USER_PROMPT_EXPANSION = "UserPromptExpansion"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_MODEL_SWITCH = "PreModelSwitch"
    POST_MODEL_SWITCH = "PostModelSwitch"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SETUP = "Setup"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    ELICITATION = "Elicitation"
    ELICITATION_RESULT = "ElicitationResult"
    CONFIG_CHANGE = "ConfigChange"
    WORKTREE_CREATE = "WorktreeCreate"
    WORKTREE_REMOVE = "WorktreeRemove"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    CWD_CHANGED = "CwdChanged"
    FILE_CHANGED = "FileChanged"
    DIRECTORY_ADDED = "DirectoryAdded"
    MESSAGE_DISPLAY = "MessageDisplay"


#: Events whose plain (non-JSON) stdout on exit 0 becomes model context (04 R33).
PLAIN_STDOUT_CONTEXT_EVENTS = {HookEvent.USER_PROMPT_SUBMIT, HookEvent.SESSION_START, HookEvent.SETUP}

#: Events where exit code 2 / ``onFailure: block`` can actually block something.
BLOCKING_CAPABLE_EVENTS = {
    HookEvent.PRE_TOOL_USE,
    HookEvent.POST_TOOL_USE,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.STOP,
    HookEvent.SUBAGENT_STOP,
    HookEvent.PRE_COMPACT,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.PRE_MODEL_SWITCH,
}

HookType = Literal["command", "prompt", "agent", "http", "mcp_tool", "callback"]

#: 04 R36 default timeouts in seconds.
DEFAULT_TIMEOUT_S: dict[str, float] = {
    "command": 600.0,
    "http": 600.0,
    "mcp_tool": 600.0,
    "prompt": 30.0,
    "agent": 60.0,
    "callback": 600.0,
}
SESSION_END_TIMEOUT_S = 1.5


@dataclass
class HookSpec:
    """One configured hook (04 §2.6). Only fields relevant to ``type`` are used."""

    type: HookType
    command: str | None = None
    args: list[str] | None = None  # exec form, no shell
    shell: Literal["bash", "powershell"] | None = None
    prompt: str | None = None
    model: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    allowed_env_vars: list[str] = field(default_factory=list)
    server: str | None = None
    tool: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    callback: Callable[[dict[str, Any]], "HookExecResult | dict[str, Any] | None"] | None = None
    if_: str | None = None  # permission-rule syntax filter (04 R32)
    timeout: float | None = None
    status_message: str | None = None
    once: bool = False
    is_async: bool = False
    async_rewake: bool = False
    on_failure: Literal["continue", "block"] = "continue"
    timeout_fails_closed: bool = False
    managed: bool = False  # came from policy settings (allowManagedHooksOnly, 04 R39)

    def dedupe_key(self) -> Any:
        """04 R34: command hooks dedupe by command string, prompt/agent by prompt+if."""
        if self.type == "command":
            return ("command", self.command, tuple(self.args or ()))
        if self.type in ("prompt", "agent"):
            return (self.type, self.prompt, self.if_)
        return (self.type, id(self))


@dataclass
class HookMatcherGroup:
    matcher: str | None
    hooks: list[HookSpec] = field(default_factory=list)


HookOutcome = Literal["ok", "block", "notice", "ignore"]


@dataclass
class HookExecResult:
    """Normalised result of one hook execution.

    * ``ok``      exit 0 (JSON parsed into the fields below, or plain context)
    * ``block``   exit 2 / onFailure=block: ``feedback`` goes to the model
    * ``notice``  other non-zero exit: ``feedback`` shown to the user only
    * ``ignore``  timed out (output discarded) or filtered
    """

    outcome: HookOutcome = "ok"
    feedback: str = ""
    decision: Literal["allow", "deny", "ask", "defer"] | None = None
    decision_reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    system_message: str | None = None
    continue_: bool = True
    stop_reason: str | None = None
    suppress_output: bool = False
    updated_tool_output: Any = None
    classifier_context: str | None = None
    specific: dict[str, Any] = field(default_factory=dict)  # remaining hookSpecificOutput
    hook_name: str = ""


@dataclass
class AggregatedHookResult:
    """Merged result of all hooks for one event (04 §4.7 ``merge``).

    First deny short-circuits; ask beats allow; last ``updated_input`` wins.
    """

    decision: Literal["allow", "deny", "ask", "defer"] | None = None
    decision_reason: str = ""
    hook_name: str = ""
    updated_input: dict[str, Any] | None = None
    additional_contexts: list[str] = field(default_factory=list)
    blocking_errors: list[str] = field(default_factory=list)  # exit-2 stderr -> model
    user_notices: list[str] = field(default_factory=list)  # non-blocking stderr -> user
    system_messages: list[str] = field(default_factory=list)
    prevent_continuation: bool = False
    stop_reason: str | None = None
    updated_tool_output: Any = None
    classifier_context: str = ""  # combined, <= 2000 UTF-16 units (04 R40)
    results: list[HookExecResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_errors) or self.decision == "deny"

