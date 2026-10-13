"""WP1 cache planning, request config and cost tests (01 R36-R45, R50)."""

from __future__ import annotations

import pytest

from harnesslab.api.accounting import CostTracker, PriceRow, cost_usd
from harnesslab.api.caching import (
    EXTENDED_TTL_BETA,
    cache_control,
    caching_disabled,
    choose_ttl,
    estimate_uncached_tail,
    is_cacheable,
    place_message_breakpoint,
)
from harnesslab.api.request import (
    build_model_request,
    detect_ultrathink,
    model_info,
    resolve_effort,
    resolve_max_tokens,
    thinking_config,
)
from harnesslab.config import Settings
from harnesslab.loop.state import LoopState, QueryParams
from harnesslab.types import Message, ThinkingBlock, ToolUseBlock, Usage


def _u(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _a(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def _marks(msgs):
    return [i for i, m in enumerate(msgs) for b in (m["content"] if isinstance(m["content"], list) else []) if "cache_control" in b]


# ------------------------------------------------------------------ breakpoints (01 R42)


def test_single_marker_on_last_block_of_last_message():
    msgs = [_u("a"), _a({"type": "text", "text": "b"}), {"role": "user", "content": [{"type": "text", "text": "c"}, {"type": "text", "text": "d"}]}]
    assert place_message_breakpoint(msgs, "5m") == 2
    assert _marks(msgs) == [2]
    assert "cache_control" not in msgs[2]["content"][0]
    assert msgs[2]["content"][1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_skip_rules():
    msgs = [
        _u("stable"),
        _a({"type": "text", "text": "x"}, {"type": "thinking", "thinking": "t", "signature": "s"}),  # thinking tail
        {"role": "user", "content": [{"type": "text", "text": "ephemeral"}], "_ephemeral": True},
        {"role": "system", "content": "sys"},
    ]
    assert not is_cacheable(msgs[1]) and not is_cacheable(msgs[2]) and not is_cacheable(msgs[3])
    assert not is_cacheable(_a({"type": "text", "text": "  "}))
    assert place_message_breakpoint(msgs, None) == 0
    assert _marks(msgs) == [0]


def test_fork_skip_write_moves_back_one():
    msgs = [_u("a"), _a({"type": "text", "text": "b"}), _u("c")]
    assert place_message_breakpoint(msgs, "1h", skip_write=True) == 1
    assert _marks(msgs) == [1]


def test_pin_second_marker_and_string_content():
    msgs = [{"role": "user", "content": "plain"}, _a({"type": "text", "text": "b"}), _u("c")]
    place_message_breakpoint(msgs, "5m", pin_index=0)
    assert _marks(msgs) == [0, 2] and isinstance(msgs[0]["content"], list)


def test_uncached_tail():
    msgs = [_u("a" * 40), _u("b" * 40)]
    assert estimate_uncached_tail(msgs, 0).tokens > 0
    assert estimate_uncached_tail(msgs, 1).tokens == 0
    assert estimate_uncached_tail(msgs, None).reason == "unstampable_tail"


def test_cache_control_shape():
    assert cache_control() == {"type": "ephemeral"}
    assert cache_control("1h", "global") == {"type": "ephemeral", "ttl": "1h", "scope": "global"}


# ------------------------------------------------------------------ TTL (01 R44) & kill switches (R45)


def test_ttl_order():
    s = Settings()
    assert choose_ttl({}, s, query_source="main", is_subscriber=True) == "1h"
    assert choose_ttl({}, s, query_source="main", is_subscriber=False) == "5m"
    assert choose_ttl({}, s, query_source="subagent", is_subscriber=True) == "5m"
    assert choose_ttl({"FORCE_PROMPT_CACHING_5M": "1", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, s, query_source="main", is_subscriber=True) == "5m"
    assert choose_ttl({"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, s, query_source="main", is_subscriber=False) == "1h"
    assert choose_ttl({"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h"}, s, query_source="subagent", is_subscriber=False) == "1h"
    st = Settings.from_dict({"promptCacheTtl": "5m", "subagentPromptCacheTtl": "1h"})
    assert choose_ttl({}, st, query_source="main", is_subscriber=True) == "5m"
    assert choose_ttl({"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, st, query_source="main", is_subscriber=False) == "1h"  # env > settings
    assert choose_ttl({}, s, query_source="subagent", is_subscriber=False, agent_ttl="1h") == "1h"
    assert choose_ttl({"ENABLE_PROMPT_CACHING_1H": "1"}, s, query_source="subagent", is_subscriber=False) == "1h"
    assert choose_ttl({}, st, query_source="subagent", is_subscriber=False, agent_ttl="5m") == "1h"  # settings > agent


def test_kill_switches():
    assert caching_disabled({"DISABLE_PROMPT_CACHING": "1"}, "any")
    assert caching_disabled({"DISABLE_PROMPT_CACHING_HAIKU": "true"}, "claude-haiku-9")
    assert not caching_disabled({"DISABLE_PROMPT_CACHING_HAIKU": "true"}, "claude-opus-9")
    assert not caching_disabled({}, "claude-opus-9")


# ------------------------------------------------------------------ limits / thinking / effort


def test_output_limits():
    info = model_info("claude-new")
    assert resolve_max_tokens(info, {}) == 32_000
    assert resolve_max_tokens(info, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "500000"}) == 128_000  # clamped (01 R36)
    assert resolve_max_tokens(info, {}, override=64_000) == 64_000
    assert model_info("claude-3-haiku-x").max_output == 4096
    assert model_info("claude-3-5-sonnet").max_output == 8192
    assert model_info("model[1m]").context_window == 1_000_000
    assert model_info("model[1m]", {"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"}).context_window == 200_000


def test_thinking_config():
    info = model_info("claude-new")
    assert thinking_config(info, {}, 32_000, enabled=True) == {"type": "adaptive"}
    assert thinking_config(info, {}, 32_000, enabled=False) == {"type": "disabled"}
    assert thinking_config(info, {"CLAUDE_CODE_DISABLE_THINKING": "1"}, 32_000, enabled=True) == {"type": "disabled"}
    assert thinking_config(info, {"MAX_THINKING_TOKENS": "0"}, 32_000, enabled=True) == {"type": "disabled"}
    no_adapt = {"CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1"}
    assert thinking_config(info, no_adapt, 32_000, enabled=True) == {"type": "enabled", "budget_tokens": 31_999}
    assert thinking_config(info, no_adapt, 32_000, enabled=True, budget=10) == {"type": "enabled", "budget_tokens": 1024}
    assert thinking_config(info, {**no_adapt, "MAX_THINKING_TOKENS": "5000"}, 32_000, enabled=True)["budget_tokens"] == 5000


def test_effort_resolution():
    info = model_info("claude-new")
    base = dict(hook=None, env_value=None, turn=None, session=None, default=None, cap=None, info=info, thinking_disabled=False)
    assert resolve_effort(**base) == "high"
    assert resolve_effort(**{**base, "session": "low", "turn": "medium"}) == "medium"
    assert resolve_effort(**{**base, "env_value": "auto", "session": "low"}) == "low"  # auto = null
    assert resolve_effort(**{**base, "env_value": "unset", "turn": "max"}) == "max"
    assert resolve_effort(**{**base, "hook": "low", "env_value": "max"}) == "low"  # hook first
    assert resolve_effort(**{**base, "turn": "max", "cap": "medium"}) == "medium"  # cap
    limited = model_info("x")
    limited.supports_effort = ("low", "medium", "high")
    assert resolve_effort(**{**base, "turn": "xhigh", "info": limited}) == "high"
    assert resolve_effort(**{**base, "turn": "max", "thinking_disabled": True}) == "high"
    assert detect_ultrathink("please UltraThink about it") and not detect_ultrathink("ultrathinking")


def test_build_request_caching_and_thinking(ctx):
    ctx.extra["is_subscriber"] = True
    ctx.model_id = "claude-new"
    items = [Message.user("hi"), Message.assistant([ToolUseBlock("t1", "Read", {})])]
    req = build_model_request(LoopState(items), ctx, QueryParams(tool_choice={"type": "tool", "name": "Read"}))
    assert EXTENDED_TTL_BETA in req.betas and req.thinking == {"type": "adaptive"}
    assert req.tool_choice == {"type": "auto"}  # demoted while thinking (01 R37)
    assert _marks(req.messages) == [len(req.messages) - 1]
    assert req.messages[-1]["content"][0]["type"] == "tool_result"  # pairing repaired
    assert req.effort == "high" and req.metadata["uncached_tail"]["tokens"] == 0
    ctx.env = {"DISABLE_PROMPT_CACHING": "1"}
    req2 = build_model_request(LoopState(items), ctx, QueryParams())
    assert _marks(req2.messages) == [] and EXTENDED_TTL_BETA not in req2.betas


def test_build_request_ultrathink_and_override(ctx):
    ctx.model_id = "claude-new"
    st = LoopState([Message.user("ultrathink: refactor this")], max_tokens_override=64_000)
    req = build_model_request(st, ctx, QueryParams())
    assert req.effort == "max" and req.max_tokens == 64_000
    from harnesslab.types import TransitionReason

    st.transition = TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY  # 01 R4: only on the first attempt
    assert build_model_request(st, ctx, QueryParams()).max_tokens == 32_000


def test_fast_mode_only_when_allowed(ctx):
    ctx.model_id = "claude-opus-x"
    ctx.extra["fast_mode"] = True
    req = build_model_request(LoopState([Message.user("hi")]), ctx, QueryParams())
    assert req.speed == "fast"
    ctx.extra["first_party"] = False
    assert build_model_request(LoopState([Message.user("hi")]), ctx, QueryParams()).speed is None


# ------------------------------------------------------------------ cost (01 R50)


def test_cost_with_cache_split_and_geo():
    row = PriceRow(input=3, output=15, cache_write_5m=3.75, cache_write_1h=6, cache_read=0.3)
    prices = {"m": row}
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_creation_input_tokens=2_000_000,
              cache_creation_5m=1_000_000, cache_creation_1h=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_usd(u, "m", prices=prices) == pytest.approx(3 + 15 + 3.75 + 6 + 0.3)
    assert cost_usd(u, "m", prices=prices, us_geo=True) == pytest.approx((3 + 15 + 3.75 + 6 + 0.3) * 1.1)
    assert cost_usd(Usage(web_search_requests=3), "m", prices=prices) == pytest.approx(0.03)
    unsplit = Usage(cache_creation_input_tokens=1_000_000)
    assert cost_usd(unsplit, "m", prices=prices) == pytest.approx(3.75)  # billed at 5 m rate


def test_cost_long_prompt_tier_and_unknown_model():
    row = PriceRow(1, 1, 1, 1, 1, long_prompt_threshold=100, long_prompt_multiplier=2.0)
    assert cost_usd(Usage(input_tokens=1_000_000), "m", prices={"m": row}) == pytest.approx(2.0)
    assert cost_usd(Usage(input_tokens=1_000_000), "totally-unknown") == pytest.approx(3.0)  # default row


def test_cost_tracker_budget():
    t = CostTracker()
    t.add(Usage(input_tokens=1_000_000), "unknown")
    t.add(Usage(input_tokens=1_000_000), "unknown")
    assert t.total_usd == pytest.approx(6.0) and t.by_model["unknown"].input_tokens == 2_000_000
    assert t.remaining(10) == pytest.approx(4.0) and t.remaining(None) is None
    assert not t.over_budget(10) and t.over_budget(6.0) and not t.over_budget(None)


def test_thinking_only_assistant_not_cacheable_in_request(ctx):
    ctx.model_id = "claude-new"
    items = [Message.user("q"), Message.assistant([ThinkingBlock("t", "sig")]), Message.user("more")]
    req = build_model_request(LoopState(items), ctx, QueryParams())
    assert _marks(req.messages) == [2]
