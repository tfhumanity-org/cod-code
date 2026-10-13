"""Optional real backend using the ``anthropic`` SDK (WP1, import-guarded).

Install with ``pip install 'harnesslab[anthropic]'``. Importing this module never
fails; constructing ``AnthropicModel`` without the SDK raises a clear error.

Maps SDK stream events to ``model.base`` stream events and SDK exceptions to
``APIError`` with an ``ErrorKind`` (01 R15, R19), parsing token counts from
prompt-too-long / overflow messages (01 R20, R26) and recording headers
(``retry-after``, ``x-should-retry``). Beta headers come from
``ModelRequest.betas`` (01 R25 latching is done by ``api.retry``).

Design choices (spec silent):

* Raw event iteration (``messages.create(stream=True)``) keeps the mapping 1:1
  with ``model.base``; objects and plain dicts are both accepted.
* The closing ``message_delta`` usage is merged with the ``message_start``
  input-side counts, so the final usage is complete (01 R50) and keeps the
  5 m / 1 h cache-creation split.
* Fields newer than the typed SDK surface (``output_config.effort``, ``speed``,
  ``context_management``, adaptive thinking) travel in ``extra_body``.
* ``stream=False`` (non-streaming fallback, 01 R14) calls ``create`` and replays
  the finished message as synthetic stream events.
* A unified-quota 429 is recognised by any ``anthropic-ratelimit-unified-*``
  response header.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from harnesslab.model.base import (
    APIError,
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    ModelRequest,
    Ping,
    StreamEvent,
)
from harnesslab.types import ErrorKind, TextBlock, ThinkingBlock, ToolUseBlock, Usage
from harnesslab.util.abort import AbortedError, AbortSignal

try:  # pragma: no cover - depends on optional extra
    import anthropic as _anthropic  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _anthropic = None

HAVE_ANTHROPIC = _anthropic is not None

_PTL = re.compile(r"(\d[\d,]*)\s*tokens?\s*>\s*(\d[\d,]*)", re.IGNORECASE)
_OVERFLOW = re.compile(r"(\d[\d,]*)\s*\+\s*(\d[\d,]*)\s*>\s*(\d[\d,]*)")
_BETA = re.compile(r"\b([a-z][a-z0-9-]*-\d{4}-\d{2}-\d{2})\b")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def convert_usage(u: Any) -> Usage:
    if u is None:
        return Usage()
    cc = _get(u, "cache_creation")
    return Usage(
        input_tokens=_get(u, "input_tokens") or 0,
        output_tokens=_get(u, "output_tokens") or 0,
        cache_creation_input_tokens=_get(u, "cache_creation_input_tokens") or 0,
        cache_read_input_tokens=_get(u, "cache_read_input_tokens") or 0,
        cache_creation_5m=(_get(cc, "ephemeral_5m_input_tokens") or 0) if cc is not None else 0,
        cache_creation_1h=(_get(cc, "ephemeral_1h_input_tokens") or 0) if cc is not None else 0,
        web_search_requests=_get(_get(u, "server_tool_use"), "web_search_requests") or 0,
    )


def _merge_usage(start: Usage, final: Usage) -> Usage:
    """Closing delta wins for every field it reports; start fills the rest."""
    return Usage(**{f: getattr(final, f) or getattr(start, f) for f in Usage.__dataclass_fields__})


class AnthropicModel:
    """``Model`` implementation over the Messages streaming API."""

    def __init__(self, model: str, *, client: Any = None, api_key: str | None = None) -> None:
        if client is None and not HAVE_ANTHROPIC:
            raise RuntimeError("the 'anthropic' package is not installed; pip install 'harnesslab[anthropic]'")
        self.name = model
        self._client = client if client is not None else _anthropic.Anthropic(api_key=api_key)  # type: ignore[union-attr]

    def _messages_api(self, request: ModelRequest) -> Any:
        return self._client.beta.messages if request.betas else self._client.messages

    def stream(self, request: ModelRequest, abort: AbortSignal | None = None) -> Iterator[StreamEvent]:
        kwargs = request_to_kwargs(request)
        api = self._messages_api(request)
        try:
            if not request.stream:
                msg = api.create(**kwargs)
                yield from _replay(msg)
                return
            raw = api.create(stream=True, **kwargs)
            start_usage = Usage()
            try:
                for sdk_event in raw:
                    if abort is not None and abort.aborted:
                        raise AbortedError(abort.reason or "aborted")
                    ev = convert_event(sdk_event)
                    if ev is None:
                        continue
                    if isinstance(ev, MessageStart):
                        start_usage = ev.usage
                    elif isinstance(ev, MessageDelta):
                        ev.usage = _merge_usage(start_usage, ev.usage)
                    yield ev
            finally:
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
        except (APIError, AbortedError):
            raise
        except Exception as exc:  # SDK errors -> classified APIError
            raise convert_error(exc) from exc


def _replay(msg: Any) -> Iterator[StreamEvent]:
    """Turn a finished (non-streamed) message into stream events (01 R14)."""
    usage = convert_usage(_get(msg, "usage"))
    yield MessageStart(_get(msg, "id", ""), _get(msg, "model", ""), Usage(input_tokens=usage.input_tokens))
    for i, b in enumerate(_get(msg, "content", []) or []):
        t = _get(b, "type")
        if t == "text":
            yield ContentBlockStart(i, TextBlock(""))
            yield ContentBlockDelta(i, "text", _get(b, "text", ""))
        elif t == "thinking":
            yield ContentBlockStart(i, ThinkingBlock(""))
            yield ContentBlockDelta(i, "thinking", _get(b, "thinking", ""))
            yield ContentBlockDelta(i, "signature", _get(b, "signature", "") or "")
        elif t == "tool_use":
            yield ContentBlockStart(i, ToolUseBlock(_get(b, "id"), _get(b, "name")))
            yield ContentBlockDelta(i, "input_json", json.dumps(_get(b, "input", {}) or {}))
        else:
            continue
        yield ContentBlockStop(i)
    yield MessageDelta(_get(msg, "stop_reason"), usage)
    yield MessageStop()


def request_to_kwargs(request: ModelRequest) -> dict[str, Any]:
    """``ModelRequest`` -> ``client.messages.create(**kwargs)`` arguments."""
    kw: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": request.messages,
    }
    if request.system:
        kw["system"] = [b.to_api() for b in request.system]
    if request.tools:
        kw["tools"] = request.tools
    if request.tool_choice:
        kw["tool_choice"] = request.tool_choice
    if request.temperature is not None:
        kw["temperature"] = request.temperature
    if request.stop_sequences:
        kw["stop_sequences"] = list(request.stop_sequences)
    if request.betas:
        kw["betas"] = list(request.betas)
    extra: dict[str, Any] = {}
    if request.thinking is not None:
        if request.thinking.get("type") in ("enabled", "disabled"):
            kw["thinking"] = request.thinking
        else:
            extra["thinking"] = request.thinking
    if request.effort:
        extra["output_config"] = {"effort": request.effort}
    if request.speed:
        extra["speed"] = request.speed
    if request.context_management:
        extra["context_management"] = request.context_management
    if extra:
        kw["extra_body"] = extra
    timeout = request.metadata.get("timeout_s")
    if timeout:
        kw["timeout"] = timeout
    return kw


def convert_event(sdk_event: Any) -> StreamEvent | None:
    """One raw SDK event -> ``model.base`` event; ``None`` for kinds we skip
    (e.g. server-tool blocks; their deltas/stops then refer to no block and are
    ignored by ``StreamAccumulator``)."""
    t = _get(sdk_event, "type")
    if t == "message_start":
        m = _get(sdk_event, "message")
        return MessageStart(_get(m, "id", ""), _get(m, "model", ""), convert_usage(_get(m, "usage")))
    if t == "content_block_start":
        idx = _get(sdk_event, "index", 0)
        b = _get(sdk_event, "content_block")
        bt = _get(b, "type")
        if bt == "text":
            return ContentBlockStart(idx, TextBlock(""))
        if bt == "thinking":
            return ContentBlockStart(idx, ThinkingBlock(""))
        if bt == "tool_use":
            return ContentBlockStart(idx, ToolUseBlock(_get(b, "id"), _get(b, "name")))
        return None
    if t == "content_block_delta":
        idx = _get(sdk_event, "index", 0)
        d = _get(sdk_event, "delta")
        dt = _get(d, "type")
        if dt == "text_delta":
            return ContentBlockDelta(idx, "text", _get(d, "text", ""))
        if dt == "thinking_delta":
            return ContentBlockDelta(idx, "thinking", _get(d, "thinking", ""))
        if dt == "signature_delta":
            return ContentBlockDelta(idx, "signature", _get(d, "signature", ""))
        if dt == "input_json_delta":
            return ContentBlockDelta(idx, "input_json", _get(d, "partial_json", ""))
        return None
    if t == "content_block_stop":
        return ContentBlockStop(_get(sdk_event, "index", 0))
    if t == "message_delta":
        return MessageDelta(_get(_get(sdk_event, "delta"), "stop_reason"), convert_usage(_get(sdk_event, "usage")))
    if t == "message_stop":
        return MessageStop()
    if t == "ping":
        return Ping()
    return None


def convert_error(exc: BaseException) -> APIError:
    """SDK exception -> classified ``APIError`` (01 R15, R19, R20, R26)."""
    from harnesslab.api.retry import error_kind_from_status

    if isinstance(exc, APIError):
        return exc
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    raw_headers = getattr(response, "headers", None) or {}
    try:
        headers = {str(k).lower(): str(v) for k, v in dict(raw_headers).items()}
    except Exception:  # pragma: no cover - exotic header containers
        headers = {}
    message = str(getattr(exc, "message", "") or exc)
    name = type(exc).__name__
    if status is None and "Timeout" in name:
        kind = ErrorKind.TIMEOUT
    elif status is None and "Connection" in name:
        kind = ErrorKind.CONNECTION
    else:
        kind = error_kind_from_status(status, message)
    actual = limit = None
    if kind == ErrorKind.CONTEXT_OVERFLOW and (m := _OVERFLOW.search(message)):
        actual, limit = _int(m.group(1)), _int(m.group(3))
    elif kind == ErrorKind.PROMPT_TOO_LONG and (m := _PTL.search(message)):
        actual, limit = _int(m.group(1)), _int(m.group(2))
    rejected = None
    if kind == ErrorKind.BETA_REJECTED and (m := _BETA.search(message)):
        rejected = m.group(1)
    unified = any(k.startswith("anthropic-ratelimit-unified") for k in headers)
    return APIError(
        kind, message, status=status, headers=headers, actual_tokens=actual, limit_tokens=limit,
        is_unified_quota=unified, rejected_beta=rejected,
    )
