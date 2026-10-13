"""NDJSON host protocol: message stream + bidirectional typed control RPC (WP5).

Implements spec 07 §2.6 (frames: inbound ``user`` with priority now|next|later,
``keep_alive``, ``update_environment_variables``; ``control_request`` /
``control_response`` / ``control_cancel_request`` both ways; outbound
``system/init``, ``assistant``, ``user``, ``stream_event``, ``result``,
``system/<subtype>``, ``tombstone``, ...), R30 (stream-json flags), R31
(``priority: now`` preempts the running turn and emits
``system/turn_preempted``), R32 + §4.8 (request ids; cancellation; direction
enforcement - agent-originated subtypes sent by a host are rejected; default
timeout 75 s, side questions 600 s), R33/R34 (subtype sets), R35 (host hook
callbacks with a generation counter; pending callbacks fail safe when the
stream closes), R36/R37 (initialize request/response fields), R38
(``session_state_changed: idle`` is the authoritative turn-over signal); 01 R59;
takeaways 07#T8, 07#T9, 01#T12; P25.

``HostServer`` drives a ``session.Engine`` (WP1) over any pair of text streams;
tests use ``io.StringIO`` pipes. Unknown subtypes get an error response (open
enums with a generic fallback, 07#T8).

Threading model
---------------
The reader (``serve``) runs on the calling thread and never blocks on a turn:
user prompts go to a priority queue drained by one worker thread. A permission
prompt raised inside a turn (worker thread) sends a ``can_use_tool`` control
request and blocks on its ``PendingRequest`` until the reader delivers the
matching ``control_response`` - or the stream closes (``fail_all``), in which
case the prompt fails closed (deny).

Design choices
--------------
* EOF on the input means the host is gone: pending requests fail immediately,
  queued prompts still run (single-shot SDK usage) but cannot ask anything.
* A stale hook callback (``generation`` changed while it was in flight) or one
  lost to a closed stream answers ``{"decision": "block"}`` for
  ``PreToolUse``/``PermissionRequest`` and ``{}`` otherwise (fail safe).
* ``AttachmentEvent`` is internal model context and produces no frame.

Deferred (documented gaps)
--------------------------
* Most of the ~70 host->CLI subtypes of R33: only ``initialize``, ``interrupt``,
  ``end_session``, ``set_permission_mode``, ``set_model``, ``get_status`` and
  ``stop_task`` are built in; others can be registered in ``HostServer.handlers``
  and the rest receive an "unsupported subtype" error response.
* ``initialize`` fields beyond ``hooks`` (R36) are stored in ``init_request`` but
  not acted on; SDK MCP tunnelling (§4.7); ``elicitation`` / dialogs; remote
  control (R41). CLI flags (R30) belong to WP1's ``cli``.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from harnesslab.permissions.types import PermissionAnswer, PermissionMode, PermissionPromptRequest, PermissionUpdate, Rule
from harnesslab.types import (
    AttachmentEvent,
    LoopEvent,
    MessageEvent,
    RawStreamEvent,
    RetryStatusEvent,
    StreamRequestStart,
    SystemNoticeEvent,
    TerminalReason,
    TombstoneEvent,
    ToolProgressEvent,
    TurnEndEvent,
    new_uuid,
)

DEFAULT_CONTROL_TIMEOUT_S = 75
SIDE_QUESTION_TIMEOUT_S = 600

HOST_TO_CLI = frozenset(
    {
        "interrupt", "end_session", "initialize", "set_permission_mode", "set_model", "set_max_thinking_tokens",
        "set_cwd", "get_status", "get_context_usage", "get_session_cost", "list_models", "get_settings",
        "list_permission_rules", "rewind_files", "rewind_conversation", "fork_conversation", "mcp_message",
        "mcp_status", "mcp_set_servers", "reload_skills", "reload_plugins", "apply_flag_settings", "side_question",
        "stop_task", "cancel_async_message",
    }
)
CLI_TO_HOST = frozenset(
    {"can_use_tool", "hook_callback", "mcp_message", "elicitation", "request_user_dialog", "oauth_token_refresh", "host_auth_token_refresh"}
)
#: Subtypes only the agent may originate; a host sending them is rejected (07 R32).
AGENT_ORIGINATED = CLI_TO_HOST - HOST_TO_CLI
PRIORITY_ORDER = {"now": 0, "next": 1, "later": 2}
FAIL_SAFE_BLOCK_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})

RESULT_SUBTYPE = {
    TerminalReason.COMPLETED: "success",
    TerminalReason.MAX_TURNS: "error_max_turns",
    TerminalReason.BUDGET_EXCEEDED: "error_max_budget_usd",
}


class ControlError(Exception):
    pass


def encode(frame: dict[str, Any]) -> str:
    """One NDJSON line."""
    return json.dumps(frame, separators=(",", ":"), default=str) + "\n"


def decode(line: str) -> dict[str, Any]:
    return json.loads(line)


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = {"type": getattr(obj, "kind", None) or type(obj).__name__}
        d.update({f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)})
        return d
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "value") and not isinstance(obj, (str, int, float, bool)):
        return obj.value
    return obj


def event_to_frames(event: LoopEvent, *, session_id: str) -> list[dict[str, Any]]:
    """Map a loop event to outbound frames (assistant/user/stream_event/system/tombstone/result)."""
    base = {"session_id": session_id}
    if isinstance(event, MessageEvent):
        m = event.message
        if m.role == "system":
            return [{"type": "system", "subtype": m.subtype or "informational", "text": m.text(), "uuid": m.uuid, **base}]
        msg = m.to_api()
        if m.role == "assistant":
            msg.update(id=m.message_id, model=m.model, stop_reason=m.stop_reason, usage=m.usage.to_dict() if m.usage else None)
        frame: dict[str, Any] = {"type": m.role, "message": msg, "uuid": m.uuid, "parent_tool_use_id": None, **base}
        if m.role == "user" and m.tool_use_result is not None:
            frame["tool_use_result"] = m.tool_use_result
        if m.is_meta:
            frame["isMeta"] = True
        return [frame]
    if isinstance(event, RawStreamEvent):
        return [{"type": "stream_event", "event": _jsonable(event.event), **base}]
    if isinstance(event, TombstoneEvent):
        return [{"type": "tombstone", "uuid": event.uuid, **base}]
    if isinstance(event, SystemNoticeEvent):
        return [{"type": "system", "subtype": event.subtype, "text": event.text, **_jsonable(event.data), **base}]
    if isinstance(event, RetryStatusEvent):
        return [{"type": "system", "subtype": "api_retry", "error": event.error, "retry_delay_ms": event.delay_ms, "attempt": event.attempt, "max_retries": event.max_attempts, **base}]
    if isinstance(event, ToolProgressEvent):
        return [{"type": "tool_progress", "tool_use_id": event.tool_use_id, "data": _jsonable(event.data), **base}]
    if isinstance(event, StreamRequestStart):
        return [{"type": "system", "subtype": "status", "status": "requesting", "model": event.model, "attempt": event.attempt, **base}]
    if isinstance(event, TurnEndEvent):
        subtype = RESULT_SUBTYPE.get(event.reason, "error_during_execution")
        return [{"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": event.turn_count, "stop_reason": event.reason.value, **base}]
    if isinstance(event, AttachmentEvent):
        return []
    return [{"type": "system", "subtype": "informational", "text": repr(event), **base}]


def _update_to_wire(u: PermissionUpdate) -> dict[str, Any]:
    d: dict[str, Any] = {"type": u.type, "destination": u.destination}
    if u.type in ("addRules", "replaceRules", "removeRules"):
        d["rules"] = [{"toolName": r.tool_name, **({"ruleContent": r.content} if r.content is not None else {})} for r in u.rules]
        d["behavior"] = u.behavior
    elif u.type == "setMode":
        d["mode"] = u.mode.value if u.mode else None
    else:
        d["directories"] = list(u.directories)
    return d


def _update_from_wire(d: dict[str, Any]) -> PermissionUpdate | None:
    t = d.get("type")
    dest = d.get("destination", "session")
    if t in ("addRules", "replaceRules", "removeRules"):
        rules = [Rule(r.get("toolName", ""), r.get("ruleContent"), d.get("behavior", "allow")) for r in d.get("rules", []) if isinstance(r, dict)]
        return PermissionUpdate(t, dest, rules=rules, behavior=d.get("behavior", "allow"))
    if t == "setMode":
        try:
            return PermissionUpdate(t, dest, mode=PermissionMode(d.get("mode")))
        except ValueError:
            return None
    if t in ("addDirectories", "removeDirectories"):
        return PermissionUpdate(t, dest, directories=list(d.get("directories", [])))
    return None  # unknown update types are ignored (open enum)


def permission_request_frame(req: PermissionPromptRequest, request_id: str) -> dict[str, Any]:
    """``can_use_tool`` control request (07 R34)."""
    body: dict[str, Any] = {
        "subtype": "can_use_tool",
        "tool_name": req.tool_name,
        "input": req.input,
        "tool_use_id": req.tool_use_id,
        "agent_id": req.agent_id,
        "permission_suggestions": [_update_to_wire(u) for u in req.permission_suggestions],
        "blocked_path": req.blocked_path,
        "decision_reason": req.decision_reason_text,
        "classifier_approvable": req.classifier_approvable,
        "suppress_always_allow_rule": req.suppress_always_allow_rule,
        "default_to_no": req.default_to_no,
        "requires_user_interaction": req.requires_user_interaction,
        "title": req.title,
        "description": req.description,
    }
    if req.decision is not None and req.decision.matched_ask_rule is not None:
        body["matched_ask_rule"] = str(req.decision.matched_ask_rule)
    return {"type": "control_request", "request_id": request_id, "request": body}


def parse_permission_answer(response: dict[str, Any]) -> PermissionAnswer:
    """Accepts the inner answer, or a whole ``control_response.response`` envelope.
    Anything unexpected becomes a deny (fail closed, 07#T9)."""
    if response.get("subtype") == "error":
        return PermissionAnswer("deny", message=f"Permission request failed: {response.get('error', 'unknown error')}")
    if response.get("subtype") == "success" and isinstance(response.get("response"), dict):
        response = response["response"]
    behavior = response.get("behavior")
    if behavior == "allow":
        updates = [u for u in (_update_from_wire(x) for x in response.get("updatedPermissions") or [] if isinstance(x, dict)) if u]
        cls = response.get("decisionClassification")
        return PermissionAnswer(
            "allow",
            updated_input=response.get("updatedInput"),
            updated_permissions=updates,
            decision_classification=cls if cls in ("user_temporary", "user_permanent", "user_reject") else None,
        )
    if behavior == "deny":
        return PermissionAnswer("deny", message=str(response.get("message", "")), interrupt=bool(response.get("interrupt")))
    return PermissionAnswer("deny", message=f"Host returned an unrecognised permission answer {behavior!r}; treated as deny.")


@dataclass
class PendingRequest:
    request_id: str
    subtype: str
    deadline: float
    result: dict[str, Any] | None = None
    error: str | None = None
    done: bool = False
    event: threading.Event = field(default_factory=threading.Event)


@dataclass
class ControlChannel:
    """Request/response matching for one direction pair (07 §4.8)."""

    write: Callable[[dict[str, Any]], None]
    pending: dict[str, PendingRequest] = field(default_factory=dict)
    generation: int = 0  # bumped on re-initialize to retire stale hook callbacks (07 R35)
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send_request(self, subtype: str, body: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        if timeout_s is None:
            timeout_s = SIDE_QUESTION_TIMEOUT_S if subtype == "side_question" else DEFAULT_CONTROL_TIMEOUT_S
        rid = new_uuid()
        p = PendingRequest(rid, subtype, deadline=timeout_s)
        with self._lock:
            if self.closed:
                raise ControlError("the host stream is closed")
            self.pending[rid] = p
        self.write({"type": "control_request", "request_id": rid, "request": {"subtype": subtype, **body}})
        if not p.event.wait(timeout_s):
            with self._lock:
                self.pending.pop(rid, None)
            raise ControlError(f"control request {subtype} timed out after {timeout_s} s")
        if p.error is not None:
            raise ControlError(p.error)
        return p.result or {}

    def on_response(self, frame: dict[str, Any]) -> None:
        resp = frame.get("response") or {}
        rid = resp.get("request_id")
        with self._lock:
            p = self.pending.pop(rid, None)
        if p is None:
            return  # late or unknown response: ignored
        if resp.get("subtype") == "error":
            p.error = str(resp.get("error") or "error")
        else:
            p.result = resp.get("response") or {}
        p.done = True
        p.event.set()

    def cancel(self, request_id: str) -> None:
        with self._lock:
            p = self.pending.pop(request_id, None)
        if p is None:
            return
        self.write({"type": "control_cancel_request", "request_id": request_id})
        p.error, p.done = "cancelled", True
        p.event.set()

    def fail_all(self, reason: str) -> None:
        """Stream closed: every pending request fails safe (07 R35)."""
        with self._lock:
            self.closed = True
            items, self.pending = list(self.pending.values()), {}
        for p in items:
            p.error, p.done = reason, True
            p.event.set()


class HostServer:
    """The engine side of the protocol: reads frames from ``inp``, writes to ``out``."""

    def __init__(self, engine: Any, inp: TextIO, out: TextIO, *, accept_env_updates: bool = False) -> None:
        self.engine = engine  # session.Engine
        self.inp = inp
        self.out = out
        self._write_lock = threading.Lock()
        self.channel = ControlChannel(self._write)
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.accept_env_updates = accept_env_updates
        self.hooks_config: dict[str, Any] = {}
        self.init_request: dict[str, Any] = {}
        self._queue: list[tuple[int, int, str]] = []
        self._seq = 0
        self._cv = threading.Condition()
        self._closed = False
        self._busy = False
        self._worker: threading.Thread | None = None
        self._sent_init = False
        self._cancelled_inbound: set[str] = set()
        ctx = getattr(engine, "ctx", None)
        self.session_id = getattr(ctx, "session_id", "") or ""

    def _write(self, frame: dict[str, Any]) -> None:
        with self._write_lock:
            self.out.write(encode(frame))
            self.out.flush()

    # ---- main loop
    def serve(self) -> int:
        self._ensure_worker()
        for line in self.inp:
            self.handle_line(line)
            if self._closed:
                break
        self.close()
        return 0

    def close(self, join_timeout: float | None = None) -> None:
        """EOF / end_session: fail pending requests, let queued turns finish."""
        self.channel.fail_all("the host stream closed")
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        if self._worker is not None:
            self._worker.join(join_timeout)

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, daemon=True, name="host-turns")
            self._worker.start()

    @property
    def busy(self) -> bool:
        return self._busy

    def handle_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            frame = decode(line)
        except ValueError:
            self._write({"type": "system", "subtype": "informational", "text": "ignored a malformed frame", "session_id": self.session_id})
            return
        t = frame.get("type")
        if t == "control_response":
            self.channel.on_response(frame)
        elif t == "control_cancel_request":
            self._cancelled_inbound.add(str(frame.get("request_id")))
        elif t == "control_request":
            self._handle_control(frame)
        elif t == "user":
            self._enqueue(frame)
        elif t == "update_environment_variables":
            self._update_env(frame)
        # keep_alive and unknown frame types are tolerated silently (07#T8)

    def _update_env(self, frame: dict[str, Any]) -> None:
        if not self.accept_env_updates:
            self._write({"type": "system", "subtype": "informational", "text": "environment updates are not accepted by this worker", "session_id": self.session_id})
            return
        ctx = getattr(self.engine, "ctx", None)
        variables = frame.get("variables") or {}
        if ctx is not None and isinstance(variables, dict):
            env = dict(ctx.env)
            env.update({str(k): str(v) for k, v in variables.items()})
            ctx.env = env

    # ---- control requests from the host
    def _respond(self, rid: str, *, response: dict[str, Any] | None = None, error: str | None = None) -> None:
        if rid in self._cancelled_inbound:
            self._cancelled_inbound.discard(rid)
            return
        body: dict[str, Any] = {"subtype": "error" if error else "success", "request_id": rid}
        if error:
            body["error"] = error
        else:
            body["response"] = response or {}
        self._write({"type": "control_response", "response": body})

    def _handle_control(self, frame: dict[str, Any]) -> None:
        rid = str(frame.get("request_id"))
        req = frame.get("request") or {}
        st = req.get("subtype")
        if st in AGENT_ORIGINATED:
            return self._respond(rid, error=f"direction not allowed: {st} may only be sent by the agent")
        handler = self.handlers.get(st) or getattr(self, f"_h_{st}", None)
        if handler is None:
            return self._respond(rid, error=f"unsupported control request subtype: {st}")
        try:
            self._respond(rid, response=handler(req) or {})
        except Exception as e:  # noqa: BLE001 - errors become error responses, never crash the reader
            self._respond(rid, error=str(e))

    def _h_initialize(self, req: dict[str, Any]) -> dict[str, Any]:
        """07 R36/R37. Re-initialize bumps the generation (stale callbacks retire)."""
        self.channel.generation += 1
        self.init_request = dict(req)
        self.hooks_config = dict(req.get("hooks") or {})
        ctx = getattr(self.engine, "ctx", None)
        pending = [
            {"request_id": p.request_id, "subtype": p.subtype}
            for p in list(self.channel.pending.values())
            if p.subtype in ("can_use_tool", "request_user_dialog")
        ]
        return {
            "commands": [getattr(s, "name", str(s)) for s in getattr(ctx, "skills", [])],
            "agents": sorted((getattr(ctx, "extra", {}) or {}).get("agents", {}) or []),
            "current_model": getattr(ctx, "model_id", None),
            "current_permission_mode": getattr(getattr(ctx, "permissions", None), "mode", PermissionMode.DEFAULT).value,
            "session_state": "running" if self._busy else "idle",
            "capabilities": {"priority_preempt": True, "hook_callbacks": True},
            "pending_permission_requests": [p for p in pending if p["subtype"] == "can_use_tool"],
            "pending_user_dialog_requests": [p for p in pending if p["subtype"] == "request_user_dialog"],
        }

    def _h_interrupt(self, req: dict[str, Any]) -> dict[str, Any]:
        self.engine.interrupt()
        return {}

    def _h_end_session(self, req: dict[str, Any]) -> dict[str, Any]:
        self._closed = True
        return {}

    def _h_set_permission_mode(self, req: dict[str, Any]) -> dict[str, Any]:
        mode = PermissionMode(req.get("mode"))
        self.engine.ctx.permissions.mode = mode
        return {"mode": mode.value}

    def _h_set_model(self, req: dict[str, Any]) -> dict[str, Any]:
        self.engine.ctx.model_id = req.get("model") or self.engine.ctx.model_id
        return {"model": self.engine.ctx.model_id}

    def _h_get_status(self, req: dict[str, Any]) -> dict[str, Any]:
        with self._cv:
            queued = len(self._queue)
        return {"session_state": "running" if self._busy else "idle", "queued": queued}

    def _h_stop_task(self, req: dict[str, Any]) -> dict[str, Any]:
        tasks = getattr(self.engine.ctx, "tasks", None)
        task = tasks.stop(str(req.get("task_id"))) if tasks is not None else None
        if task is None:
            raise ControlError(f"unknown task {req.get('task_id')!r}")
        return {"task_id": task.id, "status": task.status}

    # ---- prompts
    def _enqueue(self, frame: dict[str, Any]) -> None:
        msg = frame.get("message") or {}
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        priority = frame.get("priority") or "next"
        if priority not in PRIORITY_ORDER:
            priority = "next"
        self._ensure_worker()
        preempt = False
        with self._cv:
            self._seq += 1
            self._queue.append((PRIORITY_ORDER[priority], self._seq, str(content)))
            self._queue.sort()
            preempt = priority == "now" and self._busy
            self._cv.notify_all()
        if preempt:  # 07 R31: stop the running turn; the new prompt runs next
            self._write({"type": "system", "subtype": "turn_preempted", "session_id": self.session_id})
            self.engine.interrupt()

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if not self._queue:
                    return
                _, _, prompt = self._queue.pop(0)
                self._busy = True
            try:
                self._run_turn(prompt)
            finally:
                with self._cv:
                    self._busy = False
                # 07 R38: idle is emitted after the result frame has been flushed
                self._write({"type": "system", "subtype": "session_state_changed", "state": "idle", "session_id": self.session_id})

    def _run_turn(self, prompt: str) -> None:
        if not self._sent_init:
            self._sent_init = True
            ctx = getattr(self.engine, "ctx", None)
            self._write(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "cwd": str(getattr(ctx, "cwd", "")),
                    "model": getattr(ctx, "model_id", None),
                    "permissionMode": getattr(getattr(ctx, "permissions", None), "mode", PermissionMode.DEFAULT).value,
                    "tools": [t.name for t in getattr(ctx, "tools", [])],
                    "skills": [getattr(s, "name", str(s)) for s in getattr(ctx, "skills", [])],
                }
            )
        self._write({"type": "system", "subtype": "session_state_changed", "state": "running", "session_id": self.session_id})
        try:
            for ev in self.engine.submit(prompt):
                for f in event_to_frames(ev, session_id=self.session_id):
                    self._write(f)
        except Exception as e:  # noqa: BLE001 - report, keep serving
            self._write({"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": [str(e)], "session_id": self.session_id})

    # ---- requests to the host
    def prompter(self, req: PermissionPromptRequest) -> PermissionAnswer:
        """``interfaces.PermissionPrompter`` that asks the host via ``can_use_tool``."""
        body = permission_request_frame(req, "")["request"]
        body.pop("subtype")
        try:
            result = self.channel.send_request("can_use_tool", body)
        except ControlError as e:
            return PermissionAnswer("deny", message=f"Could not get a permission decision from the host ({e}); not running the tool.")
        return parse_permission_answer({"subtype": "success", "response": result})

    def hook_callback(self, callback_id: str, event: str, input: dict[str, Any], tool_use_id: str | None = None, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Fire a host-registered hook callback (07 R35); stale or failed calls fail safe."""
        gen = self.channel.generation
        fail_safe = {"decision": "block", "reason": "hook callback unavailable"} if event in FAIL_SAFE_BLOCK_EVENTS else {}
        try:
            out = self.channel.send_request("hook_callback", {"callback_id": callback_id, "input": input, "tool_use_id": tool_use_id}, timeout_s=timeout_s)
        except ControlError:
            return fail_safe
        if self.channel.generation != gen:
            return fail_safe  # re-initialized meanwhile: the callback belongs to a retired generation
        return out
