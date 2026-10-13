"""Per-round request construction: limits, thinking, effort, fast mode (WP1).

Implements spec 01 R36 (output limits & context windows), R37 (thinking
config; forced tool_choice demoted to auto while thinking; "ultrathink"
detection), R38 (effort resolution order + clamps), R39 (fast mode cooldowns),
R4 (output-token override only on the first attempt of a round), R46
(mid-conversation system reminders, demoted to user side if rejected).

``build_model_request`` is ``QueryDeps.build_request``: it normalizes items
(``loop.normalize.to_api_messages``), gets the system prompt from
``deps.system_prompt`` (or a minimal default), collects tool schemas from
``ctx.tools`` (deferred tools hidden unless discovered, 03 R54), stamps cache
breakpoints (``api.caching``) and fills model/thinking/effort.

Design choices (spec silent):

* The model catalog is a static table (architect gap G4): ids containing a
  legacy marker get legacy limits; a ``[1m]`` suffix selects the long window.
* ``build_model_request`` takes an extra keyword ``system_prompt`` (a
  ``SystemPromptBuilder``); ``loop.query.default_deps`` binds it.
* "ultrathink" raises the per-turn effort to the highest level the model
  supports.
* Session knobs read from ``ctx.extra``: ``thinking`` (bool, default True),
  ``thinking_budget``, ``hook_effort``, ``turn_effort``, ``is_subscriber``,
  ``first_party`` (default True), ``fast_mode`` (bool), ``agent_cache_ttl``,
  ``fork_skip_cache_write``, ``resumed``, ``latched_off_betas``.
* Mid-conversation reminders are always sent user-side (see ``loop.normalize``).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from harnesslab.api.caching import (
    EXTENDED_TTL_BETA,
    caching_disabled,
    choose_ttl,
    estimate_uncached_tail,
    place_message_breakpoint,
)
from harnesslab.config import env_int, env_truthy
from harnesslab.loop.normalize import to_api_messages
from harnesslab.loop.state import LoopState, QueryParams
from harnesslab.model.base import ModelRequest, SystemBlock
from harnesslab.runtime import SessionContext
from harnesslab.types import Message

if TYPE_CHECKING:
    from harnesslab.interfaces import SystemPromptBuilder

DEFAULT_MAX_OUTPUT = 32_000
UPPER_MAX_OUTPUT = 128_000
DEFAULT_CONTEXT_WINDOW = 200_000
LONG_CONTEXT_WINDOW = 1_000_000
THINKING_BUDGET_FLOOR = 1024
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_BETA = "effort-control"  # own placeholder name for the effort capability header (01 R38)

FAST_SHORT_RETRY_S = 20  # 01 R39
FAST_DEFAULT_COOLDOWN_S = 30 * 60
FAST_MIN_COOLDOWN_S = 10 * 60

DEFAULT_SYSTEM_TEXT = (
    "You are a coding agent working in the user's project. Use the available tools to "
    "inspect and change files, and keep the user informed with short, accurate updates."
)


@dataclass
class ModelInfo:
    """Static model catalog entry (01 R36; real catalog is remote, 01 §8)."""

    id: str
    max_output: int = DEFAULT_MAX_OUTPUT
    upper_output: int = UPPER_MAX_OUTPUT
    context_window: int = DEFAULT_CONTEXT_WINDOW
    supports_adaptive_thinking: bool = True
    supports_effort: tuple[str, ...] = EFFORT_LEVELS
    knowledge_cutoff: str = ""
    marketing_name: str = ""
    supports_thinking: bool = True
    supports_fast: bool = False
    default_effort: str | None = None


#: Legacy families with small fixed output limits (01 R36): id substring -> limit.
_LEGACY_LIMITS = {"claude-2": 4096, "claude-instant": 4096, "claude-3-haiku": 4096, "claude-3-": 4096, "claude-3-5": 8192}


def model_info(model_id: str, env: Mapping[str, str] | None = None) -> ModelInfo:
    """Static lookup (architect G4)."""
    env = env or {}
    mid = model_id.lower()
    long_ctx = "[1m]" in mid and not env_truthy(env, "CLAUDE_CODE_DISABLE_1M_CONTEXT")
    window = LONG_CONTEXT_WINDOW if long_ctx else DEFAULT_CONTEXT_WINDOW
    # longest matching legacy key wins ("claude-3-5" before "claude-3-")
    for key in sorted(_LEGACY_LIMITS, key=len, reverse=True):
        if key in mid:
            lim = _LEGACY_LIMITS[key]
            return ModelInfo(
                model_id, max_output=lim, upper_output=lim, context_window=window,
                supports_adaptive_thinking=False, supports_effort=(), supports_thinking=False,
            )
    return ModelInfo(model_id, context_window=window, supports_fast="opus" in mid)


def resolve_max_tokens(info: ModelInfo, env: Mapping[str, str], override: int | None = None) -> int:
    """01 R36/R37: ``min(explicit or recovery override or model default, upper)``.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is validated (clamped) against the upper limit."""
    explicit = env_int(env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS", 0, lo=0, hi=info.upper_output) or None
    value = override or explicit or info.max_output
    return max(1, min(value, info.upper_output))


def thinking_kill_switch(env: Mapping[str, str], settings_always_thinking: Any = None) -> bool:
    """01 R37 kill switches."""
    return (
        env_truthy(env, "CLAUDE_CODE_DISABLE_THINKING")
        or env.get("MAX_THINKING_TOKENS", "").strip() == "0"
        or settings_always_thinking is False
    )


def thinking_config(info: ModelInfo, env: Mapping[str, str], max_tokens: int, *, enabled: bool, budget: int | None = None) -> dict | None:
    """01 R37. Returns the ``thinking`` request field:

    * disabled (by caller or kill switch) -> explicit ``{"type": "disabled"}``
      (first-party behaviour; ``None`` for models without thinking at all);
    * adaptive-capable model -> ``{"type": "adaptive"}``;
    * else an explicit budget ``clamp(budget or upper-1, 1024, max_tokens-1)``.
    """
    if not info.supports_thinking:
        return None
    if not enabled or thinking_kill_switch(env):
        return {"type": "disabled"}
    if info.supports_adaptive_thinking and not env_truthy(env, "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"):
        return {"type": "adaptive"}
    env_budget = env_int(env, "MAX_THINKING_TOKENS", 0) or None
    want = budget or env_budget or info.upper_output - 1
    if max_tokens - 1 < THINKING_BUDGET_FLOOR:
        return {"type": "disabled"}  # no room for the minimum budget
    return {"type": "enabled", "budget_tokens": max(THINKING_BUDGET_FLOOR, min(want, max_tokens - 1))}


def _level(e: str) -> int:
    return EFFORT_LEVELS.index(e)


def _norm_effort(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().lower()
    return v if v in EFFORT_LEVELS else None


def resolve_effort(
    *,
    hook: str | None,
    env_value: str | None,
    turn: str | None,
    session: str | None,
    default: str | None,
    cap: str | None,
    info: ModelInfo,
    thinking_disabled: bool,
) -> str:
    """01 R38 / §4 ``resolve_effort``: first non-null of hook, env ("unset"/"auto"
    = null), turn, session, defaults, ``high``; then clamp by the org/setting cap,
    downgrade unsupported xhigh/max to high, clamp to high without thinking."""
    env_e = None if (env_value or "").strip().lower() in ("", "unset", "auto") else _norm_effort(env_value)
    e = _norm_effort(hook) or env_e or _norm_effort(turn) or _norm_effort(session) or _norm_effort(default) or "high"
    c = _norm_effort(cap)
    if c is not None and _level(e) > _level(c):
        e = c
    if e in ("xhigh", "max") and e not in info.supports_effort:
        e = "high"
    if thinking_disabled and _level(e) > _level("high"):
        e = "high"
    return e


_ULTRATHINK = re.compile(r"\bultrathink\b", re.IGNORECASE)


def detect_ultrathink(prompt: str) -> bool:
    """01 R37: the keyword is detected anywhere in the prompt, case-insensitively."""
    return bool(_ULTRATHINK.search(prompt or ""))


# ------------------------------------------------------------------ fast mode (01 R39)


@dataclass
class FastModeState:
    """Per-session fast-mode bookkeeping, kept in ``ctx.extra["fast_mode_state"]``."""

    cooldown_until: float = 0.0
    disabled_models: set[str] = field(default_factory=set)

    def active(self, model_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return model_id not in self.disabled_models and now >= self.cooldown_until

    def on_rate_limited(self, retry_after_s: float | None, now: float | None = None) -> tuple[str, float]:
        """A 429/529 in fast mode. Returns ``("wait", seconds)`` to retry in fast
        mode, or ``("cooldown", seconds)`` after entering a cooldown, continuing at
        normal speed."""
        now = time.time() if now is None else now
        if retry_after_s is not None and retry_after_s < FAST_SHORT_RETRY_S:
            return "wait", retry_after_s
        cooldown = max(retry_after_s if retry_after_s is not None else FAST_DEFAULT_COOLDOWN_S, FAST_MIN_COOLDOWN_S)
        self.cooldown_until = now + cooldown
        return "cooldown", cooldown

    def disable(self, model_id: str) -> None:
        """A 400 "fast mode unavailable / unsupported" disables it for that model."""
        self.disabled_models.add(model_id)


def fast_mode_state(ctx: SessionContext) -> FastModeState:
    st = ctx.extra.get("fast_mode_state")
    if not isinstance(st, FastModeState):
        st = FastModeState()
        ctx.extra["fast_mode_state"] = st
    return st


def fast_mode_allowed(ctx: SessionContext, info: ModelInfo) -> bool:
    """Send ``speed: fast`` only first-party, allowed model, org opted in, not cooling down."""
    return bool(
        ctx.extra.get("fast_mode")
        and ctx.extra.get("first_party", True)
        and info.supports_fast
        and ctx.settings.get("fastModeEnabled", True) is not False
        and fast_mode_state(ctx).active(info.id)
    )


# ------------------------------------------------------------------ build


def _last_user_prompt(state: LoopState) -> str:
    for it in reversed(state.items):
        if isinstance(it, Message) and it.role == "user" and not it.is_meta and not it.tool_results():
            return it.text()
    return ""


def tool_schemas(ctx: SessionContext, model_id: str) -> list[dict[str, Any]]:
    """Enabled tools; deferred tools only once discovered via ToolSearch (03 R54)."""
    out = []
    for t in ctx.tools:
        if not t.is_enabled(ctx):
            continue
        if getattr(t, "should_defer", False) and not getattr(t, "always_load", False) and t.name not in ctx.discovered_tools:
            continue
        out.append(t.api_schema(model_id))
    return out


def build_model_request(
    state: LoopState,
    ctx: SessionContext,
    params: QueryParams,
    *,
    system_prompt: "SystemPromptBuilder | None" = None,
) -> ModelRequest:
    """Assemble the ``ModelRequest`` for this round (01 R2 step 4)."""
    env = ctx.env
    model_id = state.model_override or ctx.model_id
    info = model_info(model_id, env)

    messages, report = to_api_messages(
        state.items, strict=params.strict_pairing, resumed=bool(ctx.extra.get("resumed"))
    )
    if report.actions:
        ctx.extra.setdefault("pairing_repairs", []).append(report)

    # 01 R4: the round's override applies only to the first attempt of the round
    override = state.max_tokens_override if state.transition in (None, "next_turn") else None
    max_tokens = resolve_max_tokens(info, env, override)

    enabled = bool(ctx.extra.get("thinking", True)) and ctx.settings.get("alwaysThinkingEnabled", True) is not False
    thinking = thinking_config(info, env, max_tokens, enabled=enabled, budget=ctx.extra.get("thinking_budget"))
    thinking_on = thinking is not None and thinking.get("type") != "disabled"

    tool_choice = params.tool_choice
    if thinking_on and tool_choice and tool_choice.get("type") == "tool":
        tool_choice = {"type": "auto"}  # 01 R37: forced tool demoted while thinking

    betas: list[str] = []
    effort = None
    if info.supports_effort:
        turn_effort = ctx.extra.get("turn_effort")
        if detect_ultrathink(_last_user_prompt(state)):
            turn_effort = info.supports_effort[-1]
        effort = resolve_effort(
            hook=ctx.extra.get("hook_effort"),
            env_value=env.get("CLAUDE_CODE_EFFORT_LEVEL"),
            turn=turn_effort,
            session=ctx.effort,
            default=ctx.settings.get("effortLevel") or info.default_effort,
            cap=ctx.settings.get("maxEffortLevel"),
            info=info,
            thinking_disabled=not thinking_on,
        )
        betas.append(EFFORT_BETA)

    system = system_prompt.build(ctx, ctx.tools) if system_prompt is not None else [SystemBlock(DEFAULT_SYSTEM_TEXT)]

    metadata: dict[str, Any] = {"chain_id": state.trace.chain_id, "depth": state.trace.depth}
    if not caching_disabled(env, model_id):
        ttl = choose_ttl(
            env, ctx.settings, query_source=params.query_source,
            is_subscriber=bool(ctx.extra.get("is_subscriber")), agent_ttl=ctx.extra.get("agent_cache_ttl"),
        )
        skip_write = bool(ctx.extra.get("fork_skip_cache_write"))
        idx = place_message_breakpoint(messages, ttl, skip_write=skip_write)
        if ttl == "1h":
            betas.append(EXTENDED_TTL_BETA)
        tail = estimate_uncached_tail(messages, idx, "fork_skip_write" if skip_write else "normal")
        metadata["uncached_tail"] = {"tokens": tail.tokens, "reason": tail.reason}
    for m in messages:
        m.pop("_ephemeral", None)

    latched = ctx.extra.get("latched_off_betas") or set()
    betas = [b for b in betas if b not in latched]

    return ModelRequest(
        model=model_id,
        system=system,
        messages=messages,
        tools=tool_schemas(ctx, model_id),
        max_tokens=max_tokens,
        thinking=thinking,
        effort=effort,
        tool_choice=tool_choice,
        betas=betas,
        speed="fast" if fast_mode_allowed(ctx, info) else None,
        query_source=params.query_source,
        metadata=metadata,
    )


def blocking_limit(info: ModelInfo, env: Mapping[str, str], max_output: int) -> int:
    """05 §4.1 block level (architect G5): ``context_window - min(max_out, 20k) - 3k``,
    or ``CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE``. Used by the loop's step 3 so it
    does not depend on WP4 being present."""
    override = env_int(env, "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE", 0)
    if override > 0:
        return override
    return info.context_window - min(max_output, 20_000) - 3_000

