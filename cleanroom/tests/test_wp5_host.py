"""WP5 - NDJSON host protocol and the nonce-binding permission broker (spec 07 R30-R38, 08 §4.2)."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from harnesslab.host.broker import (
    CONSENT_FIELD,
    CanonicalisationError,
    CardDecision,
    NonceStore,
    PermissionBroker,
    digest,
    serve_host_tool,
)
from harnesslab.host.protocol import (
    ControlChannel,
    ControlError,
    HostServer,
    decode,
    encode,
    event_to_frames,
    parse_permission_answer,
    permission_request_frame,
)
from harnesslab.permissions.types import Decision, PermissionMode, PermissionPromptRequest, PermissionUpdate, Rule
from harnesslab.types import Attachment, AttachmentEvent, Message, MessageEvent, TerminalReason, TombstoneEvent, TurnEndEvent


def frames(out: io.StringIO) -> list[dict]:
    return [decode(line) for line in out.getvalue().splitlines() if line.strip()]


class FakeEngine:
    def __init__(self, ctx, *, block: bool = False) -> None:
        self.ctx = ctx
        self.prompts: list[str] = []
        self.interrupts = 0
        self.block = block
        self.release = threading.Event()
        self.running = threading.Event()

    def submit(self, prompt):
        self.prompts.append(prompt)
        self.running.set()
        if self.block and len(self.prompts) == 1:
            self.release.wait(5)
            yield TurnEndEvent(TerminalReason.ABORTED_STREAMING, 1)
            return
        yield MessageEvent(Message.assistant(f"reply to {prompt}"))
        yield TurnEndEvent(TerminalReason.COMPLETED, 1)

    def interrupt(self):
        self.interrupts += 1
        self.release.set()


def req(text: str) -> PermissionPromptRequest:
    return PermissionPromptRequest("Write", {"file_path": text}, "toolu_1", Decision.ask())


# ------------------------------------------------------------------ framing


def test_encode_decode_and_event_frames():
    line = encode({"type": "keep_alive"})
    assert line.endswith("\n") and decode(line) == {"type": "keep_alive"}
    a = event_to_frames(MessageEvent(Message.assistant("hi", stop_reason="end_turn")), session_id="s")[0]
    assert a["type"] == "assistant" and a["message"]["content"][0]["text"] == "hi" and a["session_id"] == "s"
    assert event_to_frames(TurnEndEvent(TerminalReason.COMPLETED, 2), session_id="s")[0]["subtype"] == "success"
    assert event_to_frames(TurnEndEvent(TerminalReason.MAX_TURNS, 2), session_id="s")[0]["subtype"] == "error_max_turns"
    assert event_to_frames(TurnEndEvent(TerminalReason.PROMPT_TOO_LONG, 2), session_id="s")[0]["subtype"] == "error_during_execution"
    assert event_to_frames(TombstoneEvent("u1"), session_id="s")[0] == {"type": "tombstone", "uuid": "u1", "session_id": "s"}
    assert event_to_frames(AttachmentEvent(Attachment("x")), session_id="s") == []


def test_permission_frame_and_answer_parsing():
    r = req("/a")
    r.decision.matched_ask_rule = Rule("Write", None, "ask")
    r.permission_suggestions = [PermissionUpdate("addRules", "session", rules=[Rule("Write", "/a/**")])]
    f = permission_request_frame(r, "rid")
    body = f["request"]
    assert f["type"] == "control_request" and body["subtype"] == "can_use_tool" and body["matched_ask_rule"] == "Write"
    assert body["permission_suggestions"][0]["rules"] == [{"toolName": "Write", "ruleContent": "/a/**"}]
    allow = parse_permission_answer({"subtype": "success", "response": {"behavior": "allow", "updatedInput": {"x": 1}, "updatedPermissions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}, {"type": "future"}], "decisionClassification": "user_permanent"}})
    assert allow.behavior == "allow" and allow.updated_input == {"x": 1} and allow.updated_permissions[0].mode == PermissionMode.ACCEPT_EDITS
    assert len(allow.updated_permissions) == 1  # unknown update types ignored
    deny = parse_permission_answer({"behavior": "deny", "message": "no", "interrupt": True})
    assert deny.behavior == "deny" and deny.interrupt
    assert parse_permission_answer({"behavior": "maybe"}).behavior == "deny"  # fail closed
    assert parse_permission_answer({"subtype": "error", "error": "x"}).behavior == "deny"


# ------------------------------------------------------------------ control channel (07 §4.8)


def test_control_request_response_matching_and_timeout():
    ch: ControlChannel

    def write(frame):
        rid = frame["request_id"]
        threading.Timer(0.01, lambda: ch.on_response({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": {"ok": 1}}})).start()

    ch = ControlChannel(write)
    assert ch.send_request("hook_callback", {"x": 1}) == {"ok": 1}
    ch2 = ControlChannel(lambda f: None)
    t0 = time.monotonic()
    with pytest.raises(ControlError, match="timed out"):
        ch2.send_request("elicitation", {}, timeout_s=0.05)
    assert time.monotonic() - t0 < 1 and ch2.pending == {}
    ch2.on_response({"type": "control_response", "response": {"subtype": "success", "request_id": "unknown"}})  # ignored


def test_cancel_and_fail_all():
    sent = []
    ch = ControlChannel(sent.append)
    errors = []

    def asker():
        try:
            ch.send_request("can_use_tool", {}, timeout_s=5)
        except ControlError as e:
            errors.append(str(e))

    t = threading.Thread(target=asker)
    t.start()
    while not ch.pending:
        time.sleep(0.005)
    ch.cancel(next(iter(ch.pending)))
    t.join(2)
    assert errors == ["cancelled"] and sent[-1]["type"] == "control_cancel_request"
    t2 = threading.Thread(target=asker)
    t2.start()
    while not ch.pending:
        time.sleep(0.005)
    ch.fail_all("stream closed")
    t2.join(2)
    assert errors[-1] == "stream closed"
    with pytest.raises(ControlError, match="closed"):
        ch.send_request("can_use_tool", {})


# ------------------------------------------------------------------ host server


def test_serve_initialize_direction_unknown_and_turn(ctx):
    lines = [
        {"type": "control_request", "request_id": "r1", "request": {"subtype": "initialize", "hooks": {"PreToolUse": []}}},
        {"type": "control_request", "request_id": "r2", "request": {"subtype": "can_use_tool"}},
        {"type": "control_request", "request_id": "r3", "request": {"subtype": "brand_new_thing"}},
        {"type": "keep_alive"},
        {"type": "mystery_frame"},
        {"type": "update_environment_variables", "variables": {"A": "1"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
    ]
    inp = io.StringIO("".join(encode(f) for f in lines) + "not json\n")
    out = io.StringIO()
    engine = FakeEngine(ctx)
    server = HostServer(engine, inp, out)
    assert server.serve() == 0
    fs = frames(out)
    resp = {f["response"]["request_id"]: f["response"] for f in fs if f["type"] == "control_response"}
    assert resp["r1"]["subtype"] == "success" and resp["r1"]["response"]["current_permission_mode"] == "default"
    assert server.channel.generation == 1 and server.hooks_config == {"PreToolUse": []}
    assert resp["r2"]["subtype"] == "error" and "direction" in resp["r2"]["error"]
    assert resp["r3"]["subtype"] == "error" and "unsupported" in resp["r3"]["error"]
    assert "A" not in ctx.env
    assert engine.prompts == ["hello"]
    kinds = [(f["type"], f.get("subtype") or f.get("state")) for f in fs if f["type"] in ("system", "assistant", "result")]
    i_init = kinds.index(("system", "init"))
    i_result = kinds.index(("result", "success"))
    assert i_init < kinds.index(("assistant", None)) < i_result
    assert fs[-1]["subtype"] == "session_state_changed" and fs[-1]["state"] == "idle"  # R38: after the result


def test_can_use_tool_round_trip_via_prompter(ctx):
    out_lines: list[dict] = []
    server: HostServer

    class Out:
        def write(self, s):
            f = decode(s)
            out_lines.append(f)
            if f["type"] == "control_request" and f["request"]["subtype"] == "can_use_tool":
                assert f["request"]["tool_name"] == "Write"
                answer = {"type": "control_response", "response": {"subtype": "success", "request_id": f["request_id"], "response": {"behavior": "allow", "updatedInput": {"file_path": "/b"}}}}
                threading.Timer(0.01, lambda: server.handle_line(encode(answer))).start()

        def flush(self):
            pass

    server = HostServer(FakeEngine(ctx), io.StringIO(""), Out())
    ans = server.prompter(req("/a"))
    assert ans.behavior == "allow" and ans.updated_input == {"file_path": "/b"}
    server.close()
    denied = server.prompter(req("/a"))  # stream closed: fail closed
    assert denied.behavior == "deny" and "host" in denied.message


def test_priority_now_preempts_running_turn(ctx):
    out = io.StringIO()
    engine = FakeEngine(ctx, block=True)
    server = HostServer(engine, io.StringIO(""), out)
    server.handle_line(encode({"type": "user", "message": {"role": "user", "content": "long task"}}))
    assert engine.running.wait(2)
    for _ in range(100):
        if server.busy:
            break
        time.sleep(0.01)
    server.handle_line(encode({"type": "user", "message": {"role": "user", "content": "later one"}, "priority": "later"}))
    server.handle_line(encode({"type": "user", "message": {"role": "user", "content": "urgent"}, "priority": "now"}))
    server.close(join_timeout=5)
    assert engine.interrupts == 1
    assert engine.prompts == ["long task", "urgent", "later one"]
    assert any(f.get("subtype") == "turn_preempted" for f in frames(out))


def test_env_updates_accepted_when_enabled_and_handlers(ctx):
    server = HostServer(FakeEngine(ctx), io.StringIO(""), io.StringIO(), accept_env_updates=True)
    server.handle_line(encode({"type": "update_environment_variables", "variables": {"A": "1"}}))
    assert ctx.env["A"] == "1"
    server.handlers["get_widget"] = lambda r: {"w": r.get("n")}
    server.handle_line(encode({"type": "control_request", "request_id": "x", "request": {"subtype": "get_widget", "n": 3}}))
    server.handle_line(encode({"type": "control_request", "request_id": "y", "request": {"subtype": "set_permission_mode", "mode": "plan"}}))
    fs = frames(server.out)
    assert fs[-2]["response"]["response"] == {"w": 3}
    assert ctx.permissions.mode == PermissionMode.PLAN


def test_hook_callback_generation_and_fail_safe(ctx):
    server: HostServer

    class Out:
        def write(self, s):
            f = decode(s)
            if f["type"] == "control_request":
                def answer():
                    server.channel.generation += 1  # host re-initialized while in flight
                    server.handle_line(encode({"type": "control_response", "response": {"subtype": "success", "request_id": f["request_id"], "response": {"decision": "approve"}}}))

                threading.Timer(0.01, answer).start()

        def flush(self):
            pass

    server = HostServer(FakeEngine(ctx), io.StringIO(""), Out())
    assert server.hook_callback("cb1", "PreToolUse", {})["decision"] == "block"
    server.close()
    assert server.hook_callback("cb1", "PostToolUse", {}) == {}


# ------------------------------------------------------------------ broker (08 §4.2, 08#T3)


class Presenter:
    def __init__(self, *decisions: CardDecision) -> None:
        self.decisions = list(decisions)
        self.shown: list[dict] = []

    def show(self, request, canonical_input):
        self.shown.append(canonical_input)
        return self.decisions.pop(0)


def canon(tool, inp):
    if ".." in inp.get("file_path", ""):
        raise CanonicalisationError("path escapes the workspace")
    return {"file_path": inp["file_path"].rstrip("/")}


def test_nonce_single_use_and_binding():
    n = NonceStore()
    d = digest("T", {"a": 1})
    nonce = n.mint("T", d)
    assert not n.consume(nonce, "T", digest("T", {"a": 2}))  # mismatch spends it
    assert not n.consume(nonce, "T", d)
    n2 = n.mint("T", d)
    assert n.consume(n2, "T", d) and not n.consume(n2, "T", d)
    assert not n.consume(None, "T", d)


def test_broker_approval_binds_shown_input_and_server_verifies():
    p = Presenter(CardDecision("once"))
    b = PermissionBroker(p, canonicalise=canon)
    ans = b(req("/work/file/"))
    assert ans.behavior == "allow" and p.shown == [{"file_path": "/work/file"}]
    args = dict(ans.updated_input)
    assert CONSENT_FIELD in args
    ran = []
    assert serve_host_tool(b.nonces, "Write", args, lambda a: ran.append(a) or "done") == "done"
    assert ran == [{"file_path": "/work/file"}]
    again = serve_host_tool(b.nonces, "Write", args, lambda a: "x")  # replay
    assert again["is_error"] and "nothing changed" in again["content"]


def test_broker_tampered_input_and_changed_preconditions():
    b = PermissionBroker(Presenter(CardDecision("once"), CardDecision("once")), canonicalise=canon)
    args = dict(b(req("/a")).updated_input)
    args["file_path"] = "/etc/passwd"
    assert serve_host_tool(b.nonces, "Write", args, lambda a: "x")["is_error"]
    args2 = dict(b(req("/a")).updated_input)
    out = serve_host_tool(b.nonces, "Write", args2, lambda a: "x", preconditions=lambda a: False)
    assert out["is_error"] and "changed" in out["content"]


def test_broker_rejects_before_asking_and_deny_variants():
    p = Presenter(CardDecision("deny", feedback="use /b instead"), CardDecision("deny"))
    b = PermissionBroker(p, canonicalise=canon)
    early = b(req("/a/../etc"))
    assert early.behavior == "deny" and p.shown == []
    fb = b(req("/a"))
    assert fb.behavior == "deny" and fb.message == "use /b instead" and not fb.interrupt
    plain = b(req("/a"))
    assert plain.interrupt and "rejected Write" in plain.message
    assert len(b.nonces) == 0


def test_broker_only_on_card_and_session_always():
    p = Presenter(CardDecision("once", surface="notification"), CardDecision("always", surface="card"))
    b = PermissionBroker(p, canonicalise=canon, only_on_card_tools=frozenset({"Write"}))
    a = b(req("/a"))
    assert len(p.shown) == 2 and a.decision_classification == "user_permanent" and a.updated_permissions[0].rules == [Rule("Write")]
    fast = b(req("/a"))  # session cache: no card
    assert fast.behavior == "allow" and fast.decision_classification == "user_permanent" and len(p.shown) == 2
    b.mode = PermissionMode.PLAN
    with pytest.raises(IndexError):
        b(req("/a"))  # plan mode skips the fast path -> would show a card (none scripted)


def test_broker_auto_rules_and_scheduled_runs():
    b = PermissionBroker(Presenter(), auto_deny=lambda r: "no such host tool" if r.tool_name == "Write" else None)
    assert b(req("/a")).message == "no such host tool"
    b2 = PermissionBroker(Presenter(), auto_allow=lambda r: True)
    assert b2(req("/a")).behavior == "allow"
    b3 = PermissionBroker(Presenter(), scheduled_run=True, opened_by_human=False)
    d = b3(req("/a"))
    assert d.behavior == "deny" and "human approval" in d.message
    b3.routine_approved.add("Write")
    assert b3(req("/a")).behavior == "allow"
