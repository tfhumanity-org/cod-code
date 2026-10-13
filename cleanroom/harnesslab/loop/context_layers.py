"""Applying tool-requested context layers (integration).

Tools may ask for temporary context changes (03 R12, ``tools.base.ContextLayer``):
the Skill tool grants ``allowed_tools`` / ``disallowed_tools`` and may switch
``model`` / ``effort`` until the next user message (07 R8). The executor
releases layers at the right moment (serial call: at once; parallel run: when
it ends); this module turns released layers into session state, and
``expire_layers`` undoes the ``next_user_message`` ones when the user speaks.

Mapping (design choice, spec silent on representation):

| kind               | effect                                                        |
|--------------------|---------------------------------------------------------------|
| ``allowed_tools``  | allow rules (source ``command``) added to ``ctx.permissions`` |
| ``disallowed_tools``| deny rules (source ``command``)                              |
| ``model``          | ``ctx.extra["layer_model"]`` -> used as the round's model     |
| ``effort``         | ``ctx.extra["turn_effort"]`` (read by ``api.request``)        |
| ``permission_mode``| ``ctx.permissions.mode``                                      |
| ``cwd``            | ``ctx.cwd``                                                   |

Every applied layer records how to undo itself in ``ctx.extra["active_layers"]``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harnesslab.runtime import SessionContext

ACTIVE_KEY = "active_layers"


def _rules(values: Any, behavior: str) -> list[Any]:
    from harnesslab.permissions.rules import parse_rule
    from harnesslab.permissions.types import RuleSource

    out = []
    for v in values if isinstance(values, (list, tuple)) else [values]:
        try:
            out.append(parse_rule(str(v), behavior, RuleSource.COMMAND))  # type: ignore[arg-type]
        except Exception:
            continue  # a malformed grant is ignored, never widened
    return out


def apply_layer(ctx: SessionContext, layer: Any) -> None:
    """Apply one ``ContextLayer`` and remember how to undo it."""
    undo: Callable[[], None] | None = None
    kind, value = getattr(layer, "kind", None), getattr(layer, "value", None)
    if kind in ("allowed_tools", "disallowed_tools"):
        new = [r for r in _rules(value, "allow" if kind == "allowed_tools" else "deny") if r not in ctx.permissions.rules]
        ctx.permissions.rules.extend(new)

        def undo() -> None:
            ctx.permissions.rules[:] = [r for r in ctx.permissions.rules if not any(r is n for n in new)]
    elif kind == "model":
        prev = ctx.extra.get("layer_model")
        ctx.extra["layer_model"] = value

        def undo() -> None:
            ctx.extra["layer_model"] = prev
    elif kind == "effort":
        prev_effort = ctx.extra.get("turn_effort")
        ctx.extra["turn_effort"] = value

        def undo() -> None:
            ctx.extra["turn_effort"] = prev_effort
    elif kind == "permission_mode":
        from harnesslab.permissions.types import PermissionMode

        prev_mode = ctx.permissions.mode
        ctx.permissions.mode = PermissionMode(value)

        def undo() -> None:
            ctx.permissions.mode = prev_mode
    elif kind == "cwd":
        ctx.cwd = Path(str(value))
        return  # a cwd change is a real move, not a temporary layer
    else:
        return
    if getattr(layer, "until", "next_user_message") == "next_user_message" and undo is not None:
        ctx.extra.setdefault(ACTIVE_KEY, []).append(undo)


def apply_layers(ctx: SessionContext, layers: list[Any]) -> None:
    for layer in layers:
        apply_layer(ctx, layer)


def expire_layers(ctx: SessionContext) -> None:
    """Undo every ``next_user_message`` layer (newest first). Called by
    ``session.Engine.submit`` before a new user message is processed."""
    for undo in reversed(ctx.extra.pop(ACTIVE_KEY, []) or []):
        undo()
