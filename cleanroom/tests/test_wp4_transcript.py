"""WP4 - JSONL transcript DAG: append, metadata, load/resume/fork, GC (05 R36-R45, §4.7-§4.8)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from harnesslab.compaction.summarizer import boundary_row, continuation_message
from harnesslab.transcript import store as TS
from harnesslab.types import Attachment, Message, TextBlock, ToolResultBlock, ToolUseBlock


def make_store(tmp_path: Path, **kw) -> TS.TranscriptStore:
    return TS.TranscriptStore(tmp_path / "s1.jsonl", session_id="s1", cwd="/proj", **kw)


def test_parent_rules_tool_results_parent_to_assistant(tmp_path):
    st = make_store(tmp_path)
    u = Message.user("go")
    a = Message.assistant([ToolUseBlock("t1", "Read"), ToolUseBlock("t2", "Grep")], message_id="m1")
    r1 = Message.user([ToolResultBlock("t1", "one")])
    r2 = Message.user([ToolResultBlock("t2", "two")])
    att = Attachment("date", {"date": "2026-01-01"})
    a2 = Message.assistant("done", message_id="m2")
    for it in (u, a, r1, r2, att, a2):
        st.append(it)
    assert u.parent_uuid is None and a.parent_uuid == u.uuid
    assert r1.parent_uuid == a.uuid and r2.parent_uuid == a.uuid  # parallel branches
    assert r2.source_tool_assistant_uuid == a.uuid
    assert att.parent_uuid == r2.uuid and a2.parent_uuid == att.uuid
    rows = st.read_rows()
    assert [r["type"] for r in rows] == ["user", "assistant", "user", "user", "attachment", "assistant"]
    assert rows[2]["sourceToolAssistantUUID"] == a.uuid


def test_metadata_reappended_at_tail(tmp_path):
    st = make_store(tmp_path)
    st.append_metadata("custom-title", "first")
    st.append(Message.user("hello"))
    st.append_metadata("tag", "bug")
    rows = st.read_rows()
    tail_types = [r["type"] for r in rows[-2:]]
    assert sorted(tail_types) == ["custom-title", "tag"]  # all last-wins values sit at the tail
    st.append_metadata("content-replacement", {"toolUseId": "x", "stub": "s"})
    assert st.read_rows()[-1]["type"] == "content-replacement"
    meta = TS.fold_metadata(st.read_rows())
    assert meta["custom-title"] == "first" and meta["tag"] == "bug"
    assert meta["content-replacement"] == [{"toolUseId": "x", "stub": "s"}]


def test_persistence_disabled(tmp_path):
    assert not TS.persistence_enabled({"CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1"})
    assert not TS.persistence_enabled({}, no_session_persistence=True)
    st = make_store(tmp_path, enabled=False)
    m = Message.user("x")
    st.append(m)
    assert not st.path.exists() and st.last_uuid == m.uuid


def build_branchy(tmp_path):
    st = make_store(tmp_path)
    u1 = Message.user("first")
    a1 = Message.assistant("reply 1", message_id="m1")
    u2 = Message.user("second")
    a2 = Message.assistant("reply 2", message_id="m2")
    for it in (u1, a1, u2, a2):
        st.append(it)
    return st, (u1, a1, u2, a2)


def test_load_leaf_selection(tmp_path):
    st, (u1, a1, u2, a2) = build_branchy(tmp_path)
    side = Message.assistant("sidechain")
    side.is_sidechain = True
    st.append(side)
    s = TS.load_session(st.path)
    assert [m.text() for m in s.items] == ["first", "reply 1", "second", "reply 2"]  # sidechain row ignored
    assert s.session_id == "s1" and not s.forked
    s = TS.load_session(st.path, at_uuid=a1.uuid)
    assert [m.text() for m in s.items] == ["first", "reply 1"]
    st.append_metadata("last-prompt", {"leafUuid": u2.uuid})
    assert TS.load_session(st.path).items[-1].text() == "second"


def test_cycle_guard(tmp_path):
    p = tmp_path / "c.jsonl"
    rows = [
        {"type": "user", "uuid": "a", "parentUuid": "b", "sessionId": "c", "message": {"role": "user", "content": [{"type": "text", "text": "A"}]}},
        {"type": "user", "uuid": "b", "parentUuid": "a", "sessionId": "c", "message": {"role": "user", "content": [{"type": "text", "text": "B"}]}},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"torn line')
    s = TS.load_session(p)
    assert s.cycle_detected and len(s.items) == 2


def test_fork_gets_new_id_and_rewind(tmp_path):
    st, (u1, a1, u2, a2) = build_branchy(tmp_path)
    s = TS.load_session(st.path, at_uuid=a1.uuid, fork=True)
    assert s.forked and s.session_id != "s1"
    fork_path = tmp_path / f"{s.session_id}.jsonl"
    fstore = TS.write_fork(s, fork_path, cwd="/proj")
    again = TS.load_session(fork_path)
    assert again.session_id == s.session_id and [m.text() for m in again.items] == ["first", "reply 1"]
    branch = Message.user("branch")
    fstore.append(branch)
    assert branch.parent_uuid == a1.uuid
    assert TS.load_session(fork_path, at_uuid=branch.uuid).items[-2].uuid == a1.uuid
    # file without a session id cannot be adopted -> silently forks
    bare = tmp_path / "bare.jsonl"
    bare.write_text(json.dumps({"type": "user", "uuid": "x", "parentUuid": None, "message": {"role": "user", "content": []}}) + "\n")
    assert TS.load_session(bare).forked


def test_compact_boundary_stops_chain_and_replays_replacements(tmp_path):
    st, (u1, a1, u2, a2) = build_branchy(tmp_path)
    b = boundary_row(trigger="auto", pre_tokens=10, logical_parent=a2.uuid, discovered_tools=[])
    b.parent_uuid = "should-be-cleared"
    st.append(b)
    summ = continuation_message("S", "/t", auto=True)
    st.append(summ)
    a3 = Message.assistant([ToolUseBlock("t9", "Read")], message_id="m3")
    r3 = Message.user([ToolResultBlock("t9", "big output")])
    st.append(a3)
    st.append(r3)
    st.append_metadata("content-replacement", {"toolUseId": "t9", "stub": "[cleared]"})
    s = TS.load_session(st.path)
    assert b.parent_uuid is None
    assert s.items[0].subtype == "compact_boundary" and s.items[0].extra["logicalParentUuid"] == a2.uuid
    assert s.items[-1].content[0].content == "[cleared]"


def test_gc_keeps_the_right_rows(tmp_path):
    st = make_store(tmp_path, gc_min_bytes=10)
    old = [Message.user("old " + "x" * 200), Message.assistant("old reply " + "y" * 200, message_id="o")]
    for m in old:
        st.append(m)
    st.append_metadata("custom-title", "T1")
    st.append_metadata("custom-title", "T2")
    st.append_metadata("file-history-snapshot", {"messageId": "z"})
    b = boundary_row(trigger="auto", pre_tokens=1, logical_parent=old[-1].uuid, discovered_tools=[])
    st.append(b)
    assert not st.maybe_gc()  # boundary not yet followed by a real message
    st.append(continuation_message("S", "/t", auto=True))
    st.append(Message.user("new question"))
    before = os.path.getsize(st.path)
    assert st.maybe_gc()
    rows = st.read_rows()
    types = [r["type"] for r in rows]
    assert "file-history-snapshot" not in types
    assert types.count("custom-title") == 1 and TS.fold_metadata(rows)["custom-title"] == "T2"
    assert all(r.get("uuid") not in {m.uuid for m in old} for r in rows)
    assert os.path.getsize(st.path) < before
    assert [m.text() for m in TS.load_session(st.path).items][-1] == "new question"
    assert not st.maybe_gc()  # same boundary is not collected twice


def test_gc_keeps_dangling_parent_targets(tmp_path):
    rows = [
        {"type": "user", "uuid": "a", "parentUuid": None},
        {"type": "user", "uuid": "b", "parentUuid": "a"},
        {"type": "system", "subtype": "compact_boundary", "uuid": "c", "parentUuid": None},
        {"type": "user", "uuid": "d", "parentUuid": "b"},  # still points before the boundary
    ]
    keep = TS.gc_keep_indices(rows)
    assert keep == {0, 1, 2, 3}


def test_list_sessions_filters_and_preview(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    a = TS.TranscriptStore(proj / "a.jsonl", session_id="a", cwd="/real")
    a.append(Message.user("x" * 500))
    a.append_metadata("custom-title", "My title")
    side = TS.TranscriptStore(proj / "b.jsonl", session_id="b", cwd="/real")
    m = Message.user("side")
    m.is_sidechain = True
    side.append(m)
    sdk = TS.TranscriptStore(proj / "c.jsonl", session_id="c", cwd="/real", extra_fields={"entrypoint": "sdk-py"})
    sdk.append(Message.user("sdk"))
    other = TS.TranscriptStore(proj / "d.jsonl", session_id="d", cwd="/elsewhere")
    other.append(Message.user("other cwd"))
    out = TS.list_sessions(proj, real_cwd="/real")
    assert [s.session_id for s in out] == ["a"]
    assert out[0].title == "My title" and len(out[0].first_prompt_preview) == 200


def test_rewind_reminder_text():
    yes, no = TS.rewind_reminder(True), TS.rewind_reminder(False)
    assert "restored" in yes and "NOT restored" in no
    for t in (yes, no):
        assert "shell" in t and "version-control" in t
