"""Prompt-cache breakpoint planner and TTL policy (WP1).

Implements spec 01 R40-R46 and 01 §4 ``place_message_breakpoint``; takeaways
01#T8 (cache-first layout, one rolling breakpoint), 01#T9 (economic TTL);
P05.

* ``place_message_breakpoint``: exactly one marker on the last *cacheable*
  message (skip system rows, request-scoped ephemeral user rows, assistant rows
  ending in thinking/empty text); marker on the last content block; a fork that
  must not write cache moves it back one message; an optional second marker can
  pin a fork point (01 R42).
* ``choose_ttl``: first match wins, 01 R44.
* ``caching_disabled``: ``DISABLE_PROMPT_CACHING`` (+ per-family variants, 01 R45).
* ``estimate_uncached_tail``: tokens after the last breakpoint with an
  attributed reason (01 R43).

The *system prompt* split at the boundary lives in ``context.prompt``
(WP4); this module only stamps message-level markers.

Design choices (spec silent): "main sources" for the TTL env var are
``main`` and ``sdk``; the 1 h default applies to subscribers on ``main``,
``sdk``, ``classifier`` (auto mode) and ``memory_relevance`` sources. A
request-scoped row is marked by normalization with a private ``_ephemeral``
key that ``api.request`` removes before sending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from harnesslab.config import Settings, env_truthy
from harnesslab.tokens import estimate_text_tokens

TTL = Literal["5m", "1h"]
EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"
_VALID_TTLS = ("5m", "1h")
MAIN_SOURCES = frozenset({"main", "sdk"})
LONG_TTL_SOURCES = frozenset({"main", "sdk", "classifier", "memory_relevance"})


def cache_control(ttl: TTL | None = None, scope: Literal["global", "org"] | None = None) -> dict[str, Any]:
    """``{type: "ephemeral", ttl?, scope?}`` (01 R40)."""
    cc: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        cc["ttl"] = ttl
    if scope:
        cc["scope"] = scope
    return cc


def _blocks(api_message: dict[str, Any]) -> list[dict[str, Any]]:
    c = api_message.get("content")
    return c if isinstance(c, list) else []


def is_cacheable(api_message: dict[str, Any]) -> bool:
    """Can the rolling marker sit on this message? (01 R42 skip rules)"""
    if api_message.get("role") == "system" or api_message.get("_ephemeral"):
        return False
    content = api_message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    blocks = _blocks(api_message)
    if not blocks:
        return False
    if api_message.get("role") == "assistant":
        last = blocks[-1]
        if last.get("type") in ("thinking", "redacted_thinking"):
            return False
        if last.get("type") == "text" and not str(last.get("text", "")).strip():
            return False
    return True


def _stamp(api_message: dict[str, Any], cc: dict[str, Any]) -> None:
    content = api_message.get("content")
    if isinstance(content, str):
        api_message["content"] = [{"type": "text", "text": content}]
    _blocks(api_message)[-1]["cache_control"] = dict(cc)


def _prev_cacheable(api_messages: list[dict[str, Any]], start: int) -> int | None:
    i = start
    while i >= 0 and not is_cacheable(api_messages[i]):
        i -= 1
    return i if i >= 0 else None


def place_message_breakpoint(
    api_messages: list[dict[str, Any]],
    ttl: TTL | None,
    *,
    skip_write: bool = False,
    pin_index: int | None = None,
) -> int | None:
    """Mutates ``api_messages`` in place; returns the index that got the marker.

    ``skip_write`` (a fork that must not create cache entries) moves the marker
    to the previous cacheable message, so the request only *reads* the prefix
    the parent already wrote. ``pin_index`` adds an optional second marker
    (fork point / parent prompt end) when it differs from the first.
    """
    idx = _prev_cacheable(api_messages, len(api_messages) - 1)
    if idx is not None and skip_write:
        idx = _prev_cacheable(api_messages, idx - 1)
    cc = cache_control(ttl)
    if idx is not None:
        _stamp(api_messages[idx], cc)
    if pin_index is not None and pin_index != idx and 0 <= pin_index < len(api_messages):
        if is_cacheable(api_messages[pin_index]):
            _stamp(api_messages[pin_index], cc)
    return idx


def _valid(v: Any) -> TTL | None:
    return v if v in _VALID_TTLS else None  # type: ignore[return-value]


def choose_ttl(
    env: Mapping[str, str],
    settings: Settings,
    *,
    query_source: str,
    is_subscriber: bool,
    agent_ttl: TTL | None = None,
) -> TTL:
    """01 R44, first match wins."""
    main = query_source in MAIN_SOURCES
    if env_truthy(env, "FORCE_PROMPT_CACHING_5M"):
        return "5m"
    var = "CLAUDE_CODE_PROMPT_CACHE_TTL" if main else "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"
    if t := _valid(env.get(var)):
        return t
    if t := _valid(settings.get("promptCacheTtl" if main else "subagentPromptCacheTtl")):
        return t
    if t := _valid(agent_ttl):
        return t
    if env_truthy(env, "ENABLE_PROMPT_CACHING_1H"):
        return "1h"
    if is_subscriber and query_source in LONG_TTL_SOURCES:
        return "1h"
    return "5m"


def caching_disabled(env: Mapping[str, str], model_id: str) -> bool:
    """01 R45: global kill switch, or ``DISABLE_PROMPT_CACHING_<FAMILY>`` whose
    family name appears in the model id (haiku / sonnet / opus / ...)."""
    if env_truthy(env, "DISABLE_PROMPT_CACHING"):
        return True
    prefix = "DISABLE_PROMPT_CACHING_"
    mid = model_id.lower()
    for key in env:
        if key.startswith(prefix) and key[len(prefix):].lower() in mid and env_truthy(env, key):
            return True
    return False


@dataclass
class UncachedTail:
    tokens: int
    reason: str  # e.g. "normal", "fork_skip_write", "unstampable_tail", "proxy_demotion"


def estimate_uncached_tail(api_messages: list[dict[str, Any]], breakpoint_index: int | None, reason: str = "normal") -> UncachedTail:
    """01 R43: estimate tokens the server cannot read from cache (everything after
    the marker) and say why. No marker at all = the whole conversation is an
    unstampable tail."""
    start = 0 if breakpoint_index is None else breakpoint_index + 1
    tokens = sum(
        estimate_text_tokens(json.dumps(m.get("content"), default=str), is_json=True) for m in api_messages[start:]
    )
    if breakpoint_index is None and api_messages:
        reason = "unstampable_tail"
    return UncachedTail(tokens, reason)
