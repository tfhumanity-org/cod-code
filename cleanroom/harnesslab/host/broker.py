"""Host permission broker with single-use consent nonces (WP5).

Implements spec 08 §2.4 (permission request record: canonical input,
default-to-no, only-on-card, suppress-always-allow; decisions deny | once |
always | scheduled), 08 §4.2 (``can_use_tool``: auto-deny, auto-allow,
canonicalise + validate *before* asking, session rules, scheduled-run policy,
emit card, wait; on approval mint a nonce bound to ``(tool, digest(shown_input))``
and pass it in ``_consent``; the host tool server consumes it and re-checks
preconditions, refusing with "nothing changed; call again" on mismatch), 04 R48
(session-level approval caching as ``user_permanent``); takeaways 08#T3 (bind
approval to what was displayed), 04#T12; P25.

``PermissionBroker`` is an ``interfaces.PermissionPrompter`` whose UI is a
pluggable ``CardPresenter`` (a test fake, the CLI terminal, or a GUI).

Why a nonce? The card shows the *canonical* input. If the tool later ran with
different input (the model edited it, or something rewrote it), the user would
have approved something they never saw. Binding a single-use nonce to the digest
of exactly what was shown closes that time-of-check / time-of-use gap and also
stops a model from forging an "already approved" flag (08 §6).

Design choices
--------------
* ``serve_host_tool`` refusals are returned as ``{"is_error": True, "content": ...}``
  so the host tool server can forward them as tool errors verbatim.
* A card answered from a non-card surface for an ``only_on_card`` tool is shown
  again, at most ``MAX_RESHOW`` times, then treated as a denial (fail closed).
* Session "always" approvals are cached per digest of the canonical input.
* The stall / re-notify / pulse timers of 08 R3.7 are UI concerns and not modelled.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Protocol

from harnesslab.permissions.types import PermissionAnswer, PermissionMode, PermissionPromptRequest, PermissionUpdate, Rule

CONSENT_FIELD = "_consent"
MAX_RESHOW = 3
NOTHING_CHANGED = "nothing changed; call the tool again to get a fresh approval"


def digest(tool_name: str, input: dict[str, Any]) -> str:
    """Canonical digest of what the user saw (FINAL helper: pure function)."""
    canon = json.dumps({"tool": tool_name, "input": input}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


class NonceStore:
    """Single-use nonces bound to (tool, digest)."""

    def __init__(self) -> None:
        self._nonces: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def mint(self, tool_name: str, input_digest: str) -> str:
        nonce = secrets.token_urlsafe(18)
        with self._lock:
            self._nonces[nonce] = (tool_name, input_digest)
        return nonce

    def consume(self, nonce: str | None, tool_name: str, input_digest: str) -> bool:
        """True only for a known nonce bound to exactly this tool and digest.
        The nonce is spent even on a mismatch, so it can never be retried."""
        if not nonce:
            return False
        with self._lock:
            bound = self._nonces.pop(nonce, None)
        return bound == (tool_name, input_digest)

    def discard_for(self, tool_name: str, input_digest: str) -> int:
        """Drop every outstanding consent for this (tool, digest) (08 R3.9)."""
        with self._lock:
            dead = [n for n, b in self._nonces.items() if b == (tool_name, input_digest)]
            for n in dead:
                del self._nonces[n]
        return len(dead)

    def __len__(self) -> int:
        with self._lock:
            return len(self._nonces)


@dataclass
class CardDecision:
    kind: Literal["deny", "once", "always", "scheduled"]
    surface: Literal["card", "notification", "other"] = "card"
    feedback: str | None = None
    updated_input: dict[str, Any] | None = None


class CardPresenter(Protocol):
    def show(self, request: PermissionPromptRequest, canonical_input: dict[str, Any]) -> CardDecision: ...


class CanonicalisationError(ValueError):
    """Raised by a ``canonicalise`` function to reject an input before any card is shown."""


@dataclass
class PermissionBroker:
    presenter: CardPresenter
    nonces: NonceStore = field(default_factory=NonceStore)
    canonicalise: Callable[[str, dict[str, Any]], dict[str, Any]] = lambda _tool, inp: dict(inp)
    session_allow: set[str] = field(default_factory=set)  # digests approved "always" this session
    only_on_card_tools: frozenset[str] = frozenset()
    # ---- 08 R3.1-R3.6 policy hooks (WP5 additions, all optional)
    auto_deny: Callable[[PermissionPromptRequest], str | None] = lambda _r: None
    auto_allow: Callable[[PermissionPromptRequest], bool] = lambda _r: False
    force_ask_tools: frozenset[str] = frozenset()
    mode: PermissionMode = PermissionMode.DEFAULT
    is_cloud_run: bool = False
    scheduled_run: bool = False
    opened_by_human: bool = True
    routine_approved: set[str] = field(default_factory=set)
    shown: list[PermissionPromptRequest] = field(default_factory=list)  # audit trail of cards

    def __call__(self, request: PermissionPromptRequest) -> PermissionAnswer:
        tool = request.tool_name
        reason = self.auto_deny(request)
        if reason:
            return PermissionAnswer("deny", message=reason, decision_classification="user_reject")
        if self.auto_allow(request):
            return PermissionAnswer("allow", updated_input=dict(request.input), decision_classification="user_temporary")
        try:
            shown = self.canonicalise(tool, dict(request.input))
        except (CanonicalisationError, ValueError) as e:
            return PermissionAnswer("deny", message=f"The input was rejected before asking the user: {e}")
        d = digest(tool, shown)

        if d in self.session_allow and self.mode != PermissionMode.PLAN and tool not in self.force_ask_tools and not self.is_cloud_run:
            return PermissionAnswer("allow", updated_input=self._with_consent(tool, shown), decision_classification="user_permanent")
        if self.scheduled_run:
            if tool in self.routine_approved:
                return PermissionAnswer("allow", updated_input=self._with_consent(tool, shown), decision_classification="user_permanent")
            if not self.opened_by_human:
                return PermissionAnswer(
                    "deny",
                    message=f"{tool} needs a human approval and nobody is watching this scheduled run. Continue without it and mention what needed approval.",
                )

        card = replace(request, input=shown)
        self.shown.append(card)
        decision = self.presenter.show(card, shown)
        tries = 0
        while decision.kind != "deny" and decision.surface != "card" and tool in self.only_on_card_tools:
            tries += 1
            if tries > MAX_RESHOW:
                decision = CardDecision("deny", feedback="Approval must be given on the card itself.")
                break
            decision = self.presenter.show(card, shown)

        if decision.kind == "deny":
            self.nonces.discard_for(tool, d)
            if decision.feedback:
                return PermissionAnswer("deny", message=decision.feedback, interrupt=False, decision_classification="user_reject")
            return PermissionAnswer("deny", message=f"The user rejected {tool}.", interrupt=True, decision_classification="user_reject")

        final = shown
        if decision.updated_input is not None:  # the user edited the input on the card
            try:
                final = self.canonicalise(tool, dict(decision.updated_input))
            except (CanonicalisationError, ValueError) as e:
                return PermissionAnswer("deny", message=f"The edited input was rejected: {e}")
        updates: list[PermissionUpdate] = []
        classification: Literal["user_temporary", "user_permanent"] = "user_temporary"
        if decision.kind == "always":
            self.session_allow.add(digest(tool, final))
            updates = [PermissionUpdate("addRules", "session", rules=[Rule(tool)], behavior="allow")]
            classification = "user_permanent"
        elif decision.kind == "scheduled":
            self.routine_approved.add(tool)
            classification = "user_permanent"
        return PermissionAnswer("allow", updated_input=self._with_consent(tool, final), updated_permissions=updates, decision_classification=classification)

    def _with_consent(self, tool: str, shown: dict[str, Any]) -> dict[str, Any]:
        return {**shown, CONSENT_FIELD: self.nonces.mint(tool, digest(tool, shown))}


def serve_host_tool(nonces: NonceStore, tool_name: str, args: dict[str, Any], execute: Callable[[dict[str, Any]], Any], preconditions: Callable[[dict[str, Any]], bool] = lambda _a: True) -> Any:
    """Tool-server side of 08 §4.2 ``serve``: consume the nonce, re-check, then run."""
    args = dict(args)
    nonce = args.pop(CONSENT_FIELD, None)
    if not nonces.consume(nonce, tool_name, digest(tool_name, args)):
        return {"is_error": True, "content": f"This call does not match what the user approved; {NOTHING_CHANGED}."}
    if not preconditions(args):
        return {"is_error": True, "content": f"The state shown on the approval card has changed since; {NOTHING_CHANGED}."}
    return execute(args)
