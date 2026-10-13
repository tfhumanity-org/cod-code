"""WP1 anthropic adapter tests with a fake client (no SDK or network needed)."""

from __future__ import annotations

from harnesslab.model.anthropic_adapter import AnthropicModel, convert_error, convert_event, request_to_kwargs
from harnesslab.model.base import APIError, ModelRequest, StreamAccumulator, SystemBlock
from harnesslab.types import ErrorKind


class _Messages:
    def __init__(self, events=None, message=None, exc=None):
        self.events, self.message, self.exc = events, message, exc
        self.kwargs = None

    def create(self, stream=False, **kw):
        self.kwargs = {"stream": stream, **kw}
        if self.exc:
            raise self.exc
        return iter(self.events) if stream else self.message


class _Client:
    def __init__(self, messages):
        self.messages = messages
        self.beta = type("B", (), {"messages": messages})()


STREAM = [
    {"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 50, "cache_read_input_tokens": 7}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "Read"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "/x"}'}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"output_tokens": 12, "cache_creation": {"ephemeral_1h_input_tokens": 3}}},
    {"type": "message_stop"},
]


def test_stream_maps_events_and_merges_final_usage():
    model = AnthropicModel("m", client=_Client(_Messages(events=STREAM)))
    acc = StreamAccumulator()
    for ev in model.stream(ModelRequest(model="m", system=[SystemBlock("sys")])):
        acc.feed(ev)
    msg = acc.message()
    assert msg.text() == "Hi" and msg.tool_uses()[0].input == {"file_path": "/x"}
    assert msg.stop_reason == "tool_use"
    u = msg.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_input_tokens, u.cache_creation_1h) == (50, 12, 7, 3)


def test_non_streaming_replay():
    message = {"id": "m1", "model": "m", "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 2},
               "content": [{"type": "text", "text": "done"}]}
    model = AnthropicModel("m", client=_Client(_Messages(message=message)))
    acc = StreamAccumulator()
    for ev in model.stream(ModelRequest(model="m", stream=False)):
        acc.feed(ev)
    assert acc.message().text() == "done" and acc.stop_reason == "end_turn"


def test_request_to_kwargs():
    req = ModelRequest(model="m", system=[SystemBlock("s", {"type": "ephemeral"})], messages=[{"role": "user", "content": "x"}],
                       thinking={"type": "adaptive"}, effort="high", speed="fast", betas=["b1"], metadata={"timeout_s": 300})
    kw = request_to_kwargs(req)
    assert kw["system"] == [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    assert kw["extra_body"] == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}, "speed": "fast"}
    assert kw["betas"] == ["b1"] and kw["timeout"] == 300
    assert "thinking" not in kw and convert_event({"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use"}}) is None


def test_convert_error_parses_tokens_and_headers():
    class Resp:
        headers = {"Retry-After": "3", "anthropic-ratelimit-unified-status": "rejected"}

    class FakeStatusError(Exception):
        status_code = 400
        response = Resp()

    e = convert_error(FakeStatusError("prompt is too long: 210,000 tokens > 200,000 maximum"))
    assert e.kind == ErrorKind.PROMPT_TOO_LONG and (e.actual_tokens, e.limit_tokens) == (210_000, 200_000)
    assert e.retry_after_s == 3 and e.is_unified_quota
    o = convert_error(FakeStatusError("input length and `max_tokens` exceed context limit: 188059 + 20000 > 200000"))
    assert o.kind == ErrorKind.CONTEXT_OVERFLOW and (o.actual_tokens, o.limit_tokens) == (188_059, 200_000)

    class APIConnectionError(Exception):
        pass

    assert convert_error(APIConnectionError("reset")).kind == ErrorKind.CONNECTION


def test_sdk_exception_becomes_api_error():
    class Boom(Exception):
        status_code = 529

    model = AnthropicModel("m", client=_Client(_Messages(exc=Boom("Overloaded"))))
    try:
        list(model.stream(ModelRequest(model="m")))
    except APIError as e:
        assert e.kind == ErrorKind.OVERLOADED
    else:  # pragma: no cover
        raise AssertionError("expected APIError")
