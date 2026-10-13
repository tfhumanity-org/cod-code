"""WP1 normalization & pairing repair tests (01 R55-R57, 02 R30)."""

from __future__ import annotations

import pytest

from harnesslab.loop.normalize import (
    MEDIA_REMOVED_TEXT,
    ORPHAN_REMOVED_TEXT,
    PairingError,
    cap_media,
    merge_consecutive_user,
    repair_tool_pairing,
    strip_lone_surrogates,
    to_api_messages,
)
from harnesslab.types import (
    SYNTHETIC_RESULT_TEXT,
    ATTACHMENT_RENDERERS,
    ApiErrorDetail,
    Attachment,
    ErrorKind,
    ImageBlock,
    Message,
    SyntheticReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _use(id_: str) -> ToolUseBlock:
    return ToolUseBlock(id_, "Read", {})


def _res(id_: str, text: str = "ok") -> ToolResultBlock:
    return ToolResultBlock(id_, text)


def test_merge_consecutive_users_results_first():
    a = Message.user("reminder", is_meta=True)
    b = Message.user([_res("t1")])
    merged = merge_consecutive_user([Message.assistant([_use("t1")]), a, b])
    assert len(merged) == 2
    assert [blk.type for blk in merged[1].content] == ["tool_result", "text"]


def test_drop_empty_text_and_placeholder():
    msgs, _ = to_api_messages([Message.user([TextBlock("   "), TextBlock("hi")]), Message.assistant([TextBlock("")])])
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]
    assert msgs[1]["content"] == [{"type": "text", "text": "(no content)"}]


def test_lone_surrogates_stripped():
    assert strip_lone_surrogates("ok\ud800ay") == "okay"
    msgs, _ = to_api_messages([Message.user("bad\udfffchar")])
    assert msgs[0]["content"][0]["text"] == "badchar"


def test_attachments_rendered_as_meta_reminders():
    ATTACHMENT_RENDERERS["wp1_test_kind"] = lambda p: f"note: {p['x']}"
    msgs, _ = to_api_messages([Message.user("hi"), Attachment("wp1_test_kind", {"x": 1})])
    assert len(msgs) == 1  # merged into the user turn
    text = msgs[0]["content"][1]["text"]
    assert text.startswith("<system-reminder>") and "note: 1" in text  # 02 R30


def test_system_rows_and_api_errors_not_sent_and_meta_wrapped():
    items = [
        Message.user("q"),
        Message.system("compacted", subtype="informational"),
        Message.assistant("boom", is_api_error=True, api_error=ApiErrorDetail(ErrorKind.SERVER_ERROR, "x")),
        Message.user("keep going", is_meta=True),
    ]
    msgs, _ = to_api_messages(items)
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert msgs[0]["content"][1]["text"].startswith("<system-reminder>")


def test_unsigned_thinking_stripped_signed_kept():
    msgs, _ = to_api_messages([
        Message.user("q"),
        Message.assistant([ThinkingBlock("secret", ""), ThinkingBlock("ok", "sig"), TextBlock("answer")]),
    ])
    types = [b["type"] for b in msgs[1]["content"]]
    assert types == ["thinking", "text"] and msgs[1]["content"][0]["signature"] == "sig"


def test_pairing_missing_result_gets_missing_text():
    msgs, report = repair_tool_pairing([Message.user("go"), Message.assistant([_use("t1"), _use("t2")]), Message.user([_res("t1")])])
    results = msgs[2].tool_results()
    assert [r.tool_use_id for r in results] == ["t1", "t2"]
    assert results[1].is_error and results[1].content == SYNTHETIC_RESULT_TEXT[SyntheticReason.MISSING]
    assert report.actions and report.fingerprint


def test_pairing_missing_result_at_end_creates_user_row():
    msgs, _ = repair_tool_pairing([Message.user("go"), Message.assistant([_use("t1")])])
    assert msgs[-1].role == "user" and msgs[-1].tool_results()[0].tool_use_id == "t1"


def test_pairing_duplicate_ids_and_orphans():
    msgs, report = repair_tool_pairing([
        Message.user("go"),
        Message.assistant([_use("t1")]),
        Message.user([_res("t1"), _res("ghost")]),
        Message.assistant([_use("t1")]),  # duplicate id: dropped
        Message.user([_res("t1", "dup")]),  # misplaced now: dropped
    ])
    assert [b.id for m in msgs for b in m.tool_uses()] == ["t1"]
    all_results = [r.tool_use_id for m in msgs for r in m.tool_results()]
    assert all_results == ["t1"]
    assert any("duplicate" in a for a in report.actions) and any("orphan" in a for a in report.actions)


def test_pairing_leading_orphan_on_resume():
    msgs, _ = repair_tool_pairing([Message.user([_res("gone")]), Message.assistant("hi")], resumed=True)
    assert msgs[0].content[0].text == ORPHAN_REMOVED_TEXT


def test_pairing_strict_mode_raises():
    with pytest.raises(PairingError):
        repair_tool_pairing([Message.assistant([_use("t1")])], strict=True)
    ok, report = repair_tool_pairing([Message.assistant([_use("t1")]), Message.user([_res("t1")])], strict=True)
    assert not report.actions


def test_items_not_mutated():
    a = Message.assistant([_use("t1")])
    items = [Message.user("go"), a]
    to_api_messages(items)
    assert a.content == [_use("t1")] and len(items) == 2


def test_cap_media_replaces_oldest():
    imgs = [Message.user([ImageBlock("image/png", "AAAA")]) for _ in range(3)]
    nested = Message.user([ToolResultBlock("t", [ImageBlock("image/png", "BBBB")])])
    out = cap_media(imgs + [nested], max_blocks=2)
    assert out[0].content[0].text == MEDIA_REMOVED_TEXT and out[1].content[0].text == MEDIA_REMOVED_TEXT
    assert isinstance(out[2].content[0], ImageBlock)
    assert isinstance(out[3].content[0].content[0], ImageBlock)
    assert isinstance(imgs[0].content[0], ImageBlock)  # input untouched
    by_bytes = cap_media(imgs, max_blocks=10, max_bytes=8)
    assert by_bytes[0].content[0].text == MEDIA_REMOVED_TEXT and isinstance(by_bytes[1].content[0], ImageBlock)
