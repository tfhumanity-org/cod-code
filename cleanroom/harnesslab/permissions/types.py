"""Permission vocabulary: modes, rules, typed decisions (FINAL - architect).

Shared by tools (``Tool.check_permissions``), the permission pipeline (WP3), the
host protocol / broker (WP5) and agents (WP5). Fully implemented.

Spec coverage
-------------
* 04 §2.1  ``PermissionMode`` (default, acceptEdits, plan, dontAsk,
           bypassPermissions, auto) + ``bubble`` for subagents (06 §2.5).
* 04 §2.2  ``Decision`` = behavior + typed ``DecisionReason`` (04#T1: "type the
           reason; build it first").
* 04 §2.3  ``Rule`` with source and behavior; precedence by behavior
           (deny > ask > allow) regardless of source.
* 04 §2.5 / 07 §2.7  ``PermissionUpdate`` persisted changes.
* 04 §2.10 / 07 R34  ``PermissionPromptRequest`` / ``PermissionAnswer`` (host
           ``can_use_tool`` payload).
* 04 R14   ``SEVERITY`` ranks for combining subcommand decisions.
* 04 §4.4  ``ClassifierVerdict``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Union


class PermissionMode(str, enum.Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"
    AUTO = "auto"
    BUBBLE = "bubble"  # subagent: route prompts to the parent (06 R20)


Behavior = Literal["allow", "deny", "ask", "passthrough"]
RuleBehavior = Literal["allow", "deny", "ask"]

#: 04 R14 / §5 severity ranks.
SEVERITY: dict[str, int] = {"deny": 3, "ask": 2, "passthrough": 1, "allow": 0}


class RuleSource(str, enum.Enum):
    """04 §2.3 aggregated rule sources."""

    USER = "userSettings"
    PROJECT = "projectSettings"
    LOCAL = "localSettings"
    FLAG = "flagSettings"
    POLICY = "policySettings"
    CLI_ARG = "cliArg"
    COMMAND = "command"
    SESSION = "session"
    TOOLS_NARROWING = "toolsNarrowing"
    MCP_SERVER_POLICY = "mcpServerPolicy"
    HOST_CREDENTIAL = "hostCredential"


@dataclass(frozen=True)
class Rule:
    """``Tool`` or ``Tool(content)``. ``content=None`` means whole-tool rule
    (``Tool()``/``Tool(*)`` normalise to it, 04 §2.3)."""

    tool_name: str
    content: str | None = None
    behavior: RuleBehavior = "allow"
    source: RuleSource = RuleSource.SESSION

    def __str__(self) -> str:
        if self.content is None:
            return self.tool_name
        esc = self.content.replace("(", "\\(").replace(")", "\\)")
        return f"{self.tool_name}({esc})"


# --------------------------------------------------------------- decision reasons


@dataclass
class RuleReason:
    rule: Rule
    type: ClassVar[str] = "rule"


@dataclass
class ModeReason:
    mode: PermissionMode
    type: ClassVar[str] = "mode"


@dataclass
class HookReason:
    hook_name: str
    reason: str = ""
    type: ClassVar[str] = "hook"


@dataclass
class ClassifierReason:
    stage: int
    reason: str = ""
    category: str | None = None
    fast_path: str | None = None  # "allowlist" | "acceptEdits" | "hardcoded"
    type: ClassVar[str] = "classifier"


@dataclass
class SafetyCheckReason:
    """A hazard hit. ``circuit_breaker`` keys into the hazard table (04 §2.4)."""

    message: str
    classifier_approvable: bool = True
    circuit_breaker: str | None = None
    type: ClassVar[str] = "safetyCheck"


@dataclass
class SubcommandResultsReason:
    per_subcommand: list["Decision"] = field(default_factory=list)
    type: ClassVar[str] = "subcommandResults"


@dataclass
class WorkingDirReason:
    path: str
    type: ClassVar[str] = "workingDir"


@dataclass
class AsyncAgentReason:
    agent_id: str
    type: ClassVar[str] = "asyncAgent"


@dataclass
class OtherReason:
    text: str = ""
    type: ClassVar[str] = "other"


DecisionReason = Union[
    RuleReason,
    ModeReason,
    HookReason,
    ClassifierReason,
    SafetyCheckReason,
    SubcommandResultsReason,
    WorkingDirReason,
    AsyncAgentReason,
    OtherReason,
]


@dataclass
class PermissionUpdate:
    """Persisted permission change (04 §2.5). ``type`` selects which fields matter."""

    type: Literal["addRules", "replaceRules", "removeRules", "setMode", "addDirectories", "removeDirectories"]
    destination: Literal["userSettings", "projectSettings", "localSettings", "session", "cliArg"] = "session"
    rules: list[Rule] = field(default_factory=list)
    behavior: RuleBehavior = "allow"
    mode: PermissionMode | None = None
    directories: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """A permission verdict. Layers may only tighten it (04#T2).

    The boolean flags record *why* an ask is mandatory so auto mode knows not to
    send it to the classifier (04 §4.4 ``mandatory``).
    """

    behavior: Behavior
    reason: DecisionReason = field(default_factory=OtherReason)
    updated_input: dict[str, Any] | None = None
    message: str | None = None
    suggestions: list[PermissionUpdate] = field(default_factory=list)
    hook_ask_floor: bool = False
    plan_floor: bool = False
    org_ceiling: bool = False
    workflow_consent: bool = False
    matched_ask_rule: Rule | None = None
    blocked_path: str | None = None

    @staticmethod
    def allow(reason: DecisionReason | None = None, updated_input: dict[str, Any] | None = None) -> "Decision":
        return Decision("allow", reason or OtherReason(), updated_input)

    @staticmethod
    def deny(message: str, reason: DecisionReason | None = None) -> "Decision":
        return Decision("deny", reason or OtherReason(message), message=message)

    @staticmethod
    def ask(reason: DecisionReason | None = None, message: str | None = None, **flags: Any) -> "Decision":
        return Decision("ask", reason or OtherReason(message or ""), message=message, **flags)

    @staticmethod
    def passthrough(reason: DecisionReason | None = None) -> "Decision":
        return Decision("passthrough", reason or OtherReason())

    @property
    def severity(self) -> int:
        return SEVERITY[self.behavior]


@dataclass
class PermissionContext:
    """Session permission state: mode + aggregated rules + extra directories."""

    mode: PermissionMode = PermissionMode.DEFAULT
    rules: list[Rule] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)
    pre_plan_mode: PermissionMode | None = None  # 04 R4
    bypass_available: bool = False  # launched with --dangerously-skip-permissions (04 R3)
    headless: bool = False  # no prompts possible: residual asks become denies (04 R30)

    def rules_with(self, behavior: RuleBehavior) -> list[Rule]:
        return [r for r in self.rules if r.behavior == behavior]


@dataclass
class ClassifierVerdict:
    """Result of the auto-mode judge (04 §4.4). ``unavailable`` means no verdict at
    all (outage / timeout), which counts toward the no-verdict streak (04 R22)."""

    block: bool
    stage: int = 1
    category: str | None = None
    reason: str = ""
    unavailable: bool = False


# --------------------------------------------------------------- host prompt


@dataclass
class PermissionPromptRequest:
    """Payload of a host ``can_use_tool`` control request (04 §2.10, 07 R34)."""

    tool_name: str
    input: dict[str, Any]
    tool_use_id: str
    decision: Decision
    agent_id: str | None = None
    permission_suggestions: list[PermissionUpdate] = field(default_factory=list)
    blocked_path: str | None = None
    decision_reason_text: str = ""
    classifier_approvable: bool = False
    suppress_always_allow_rule: bool = False
    default_to_no: bool = False
    requires_user_interaction: bool = False
    title: str = ""
    description: str = ""


@dataclass
class PermissionAnswer:
    """Host/user answer (07 §2.7)."""

    behavior: Literal["allow", "deny"]
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[PermissionUpdate] = field(default_factory=list)
    message: str = ""
    interrupt: bool = False
    decision_classification: Literal["user_temporary", "user_permanent", "user_reject"] | None = None
