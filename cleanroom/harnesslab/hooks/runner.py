"""Hook dispatch: config loading, parallel execution, exit-code semantics, merge (WP3).

Implements spec 04 §2.6-§2.9, R31-R40 and §4.7; takeaway 04#T9.

* ``load_hook_config(settings)`` -> ``{HookEvent: [HookMatcherGroup]}``;
  honours ``disableAllHooks`` / ``allowManagedHooksOnly`` (R39).
* ``HookRunner.run``: select groups by matcher, filter by ``if``, dedupe (R34),
  run in parallel on the *same original input*, merge (first deny
  short-circuits, ask beats allow, last ``updatedInput`` wins).
* ``run_one``: command hooks via ``subprocess`` with JSON on stdin; exit 0 ->
  JSON or plain context (UserPromptSubmit/SessionStart/Setup); exit 2 -> block
  with stderr to the model; other -> user notice; ``onFailure: block`` and
  ``timeoutFailsClosed`` (R33, R36). Timed-out output is discarded. Exec-form
  args expand only ``CLAUDE_PROJECT_DIR``, ``CLAUDE_PLUGIN_ROOT``,
  ``CLAUDE_PLUGIN_DATA`` (R38). ``callback`` hooks call Python directly (design
  choice, see hooks.types). ``prompt``/``agent`` hooks call ``ctx.model`` with
  structured output ``{ok, reason, impossible?}``, halving the transcript on
  overflow (R37). ``http`` hooks only to ``allowedHttpHookUrls`` with ``$VAR``
  expansion limited to ``allowedEnvVars``. ``async`` hooks run in a background
  thread; ``asyncRewake`` enqueues a notification on exit 2.
* ``common_payload`` builds the 04 §2.8 stdin object.
* PostToolUse ``classifierContext`` (all hooks combined, <= 2000 UTF-16 units)
  is appended to ``ctx.extra["classifier_context"]`` for the auto-mode judge
  (R40).
* 07 R35 callback hooks: a host's ``hook_callback`` is wired as a ``callback``
  ``HookSpec`` whose function returns the same JSON shape as command stdout.

Design choices: a ``callback`` / ``prompt`` / ``http`` hook that times out keeps
running in a daemon thread (Python cannot kill threads) but its result is
discarded. ``defer`` decisions merge like a stronger ``ask``; the permission
pipeline treats them as deny (spec 04 §8: unsupported -> deny).
Deferred: ``agent`` hooks run as a single-turn prompt hook (no multi-turn
subagent with transcript Read access); the ``{async: true}`` stdout handshake is
accepted but the process is still awaited (configured ``async`` hooks do run in
the background).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from harnesslab.config import Settings, SettingsSource, env_int
from harnesslab.hooks.matcher import MATCH_FIELD, event_match_values, if_filter_matches, matcher_matches
from harnesslab.hooks.types import (
    BLOCKING_CAPABLE_EVENTS,
    DEFAULT_TIMEOUT_S,
    PLAIN_STDOUT_CONTEXT_EVENTS,
    SESSION_END_TIMEOUT_S,
    AggregatedHookResult,
    HookEvent,
    HookExecResult,
    HookMatcherGroup,
    HookSpec,
)
from harnesslab.model.base import APIError, ModelRequest, StreamAccumulator, SystemBlock
from harnesslab.runtime import SessionContext
from harnesslab.types import ErrorKind, Message

log = logging.getLogger(__name__)

CLASSIFIER_CONTEXT_CAP = 2000  # UTF-16 code units (04 R40)
SESSION_END_TIMEOUT_MAX_S = 60.0
#: Events whose command hooks receive ``CLAUDE_ENV_FILE`` (04 R38).
ENV_FILE_EVENTS = {HookEvent.SESSION_START, HookEvent.CWD_CHANGED, HookEvent.FILE_CHANGED}
#: Events where ``timeoutFailsClosed`` turns a timeout into a block (04 R36).
TIMEOUT_FAIL_CLOSED_EVENTS = {HookEvent.PRE_TOOL_USE, HookEvent.USER_PROMPT_SUBMIT}
#: The only variables expanded inside exec-form ``args`` (04 R38).
EXPANDABLE_ARG_VARS = ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")

#: Own wording: instructions for prompt/agent hooks (04 R37).
PROMPT_HOOK_SYSTEM = (
    "You are a verification step inside a coding agent's hook system. Read the request "
    "and the event data, then answer with a single JSON object and nothing else: "
    '{"ok": true|false, "reason": "<one sentence>", "impossible": true|false}. '
    "Use ok=false only when the condition you were asked to check is not met, and cite the "
    "concrete evidence from the event data or transcript in the reason. Set impossible=true "
    "only when the goal can never be met, so the agent may stop trying."
)

_DECISION_RANK = {None: 0, "allow": 1, "ask": 2, "defer": 3, "deny": 4}


# ------------------------------------------------------------------ configuration


def _spec_from_dict(d: dict[str, Any], *, managed: bool) -> HookSpec | None:
    typ = d.get("type", "command")
    if typ not in ("command", "prompt", "agent", "http", "mcp_tool"):
        log.warning("ignoring hook with unknown type %r", typ)
        return None
    return HookSpec(
        type=typ,
        command=d.get("command"),
        args=list(d["args"]) if isinstance(d.get("args"), list) else None,
        shell=d.get("shell"),
        prompt=d.get("prompt"),
        model=d.get("model"),
        url=d.get("url"),
        headers=dict(d.get("headers", {}) or {}),
        allowed_env_vars=list(d.get("allowedEnvVars", []) or []),
        server=d.get("server"),
        tool=d.get("tool"),
        tool_input=dict(d.get("input", {}) or {}),
        if_=d.get("if"),
        timeout=float(d["timeout"]) if d.get("timeout") is not None else None,
        status_message=d.get("statusMessage"),
        once=bool(d.get("once", False)),
        is_async=bool(d.get("async", False)),
        async_rewake=bool(d.get("asyncRewake", False)),
        on_failure="block" if d.get("onFailure") == "block" else "continue",
        timeout_fails_closed=bool(d.get("timeoutFailsClosed", False)),
        managed=managed,
    )


def load_hook_config(settings: Settings) -> dict[HookEvent, list[HookMatcherGroup]]:
    """Read ``hooks`` from every settings layer (04 §2.6). Hooks from policy
    settings are marked ``managed`` so ``allowManagedHooksOnly`` can keep just
    those (R39). ``disableAllHooks`` is honoured at run time, not here."""
    out: dict[HookEvent, list[HookMatcherGroup]] = {}
    by_name = {e.value: e for e in HookEvent}
    for src in SettingsSource:
        hooks = settings.get_from(src, "hooks", {}) or {}
        if not isinstance(hooks, dict):
            continue
        for name, groups in hooks.items():
            event = by_name.get(name)
            if event is None or not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                specs = [_spec_from_dict(h, managed=src == SettingsSource.POLICY) for h in g.get("hooks", []) if isinstance(h, dict)]
                out.setdefault(event, []).append(HookMatcherGroup(g.get("matcher"), [h for h in specs if h is not None]))
    return out


def common_payload(event: HookEvent, ctx: SessionContext, **fields: Any) -> dict[str, Any]:
    """The 04 §2.8 stdin object shared by every event; event fields are added on top."""
    p: dict[str, Any] = {
        "session_id": ctx.session_id,
        "transcript_path": str(ctx.transcript_path),
        "cwd": str(ctx.cwd),
        "permission_mode": ctx.permissions.mode.value,
        "hook_event_name": event.value,
    }
    if ctx.extra.get("prompt_id"):
        p["prompt_id"] = ctx.extra["prompt_id"]
    if ctx.agent_id is not None:
        p["agent_id"] = ctx.agent_id
        p["agent_type"] = ctx.agent_type
    if ctx.effort:
        p["effort"] = {"level": ctx.effort}
    p.update(fields)
    return p


# ------------------------------------------------------------------ output parsing


def _hook_output_from_dict(event: HookEvent, data: dict[str, Any]) -> HookExecResult:
    """Map 04 §2.9 JSON fields onto a ``HookExecResult``."""
    r = HookExecResult()
    if data.get("async") is True:
        r.specific["async"] = True
        return r
    if data.get("continue") is False:
        r.continue_ = False
        r.stop_reason = data.get("stopReason")
    r.suppress_output = bool(data.get("suppressOutput", False))
    if isinstance(data.get("systemMessage"), str):
        r.system_message = data["systemMessage"]
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    top = data.get("decision")
    if top == "block":
        r.outcome = "block"
        r.feedback = reason or "Blocked by hook."
        if event in (HookEvent.PRE_TOOL_USE, HookEvent.PRE_MODEL_SWITCH):
            r.decision, r.decision_reason = "deny", r.feedback
    elif top == "approve" and event in (HookEvent.PRE_TOOL_USE, HookEvent.PRE_MODEL_SWITCH):
        r.decision, r.decision_reason = "allow", reason
    hso = data.get("hookSpecificOutput")
    if isinstance(hso, dict):
        rest = dict(hso)
        rest.pop("hookEventName", None)
        pd = rest.pop("permissionDecision", None)
        if pd in ("allow", "deny", "ask", "defer"):
            r.decision = pd
            r.decision_reason = str(rest.pop("permissionDecisionReason", "") or reason)
        if isinstance(rest.get("updatedInput"), dict):
            r.updated_input = rest.pop("updatedInput")
        if isinstance(rest.get("additionalContext"), str):
            r.additional_context = rest.pop("additionalContext")
        if isinstance(rest.get("classifierContext"), str):
            r.classifier_context = rest.pop("classifierContext")
        for key in ("updatedToolOutput", "updatedMCPToolOutput"):
            if key in rest:
                r.updated_tool_output = rest.pop(key)
        pr = rest.get("decision")
        if event == HookEvent.PERMISSION_REQUEST and isinstance(pr, dict):
            if pr.get("behavior") == "allow":
                r.decision = "allow"
                if isinstance(pr.get("updatedInput"), dict):
                    r.updated_input = pr["updatedInput"]
            elif pr.get("behavior") == "deny":
                r.decision = "deny"
                r.decision_reason = str(pr.get("message", "") or "")
        r.specific = rest
    return r


def parse_hook_stdout(event: HookEvent, stdout: str) -> HookExecResult:
    """Map 04 §2.9 JSON (top-level + hookSpecificOutput) to ``HookExecResult``.

    Raises ``ValueError`` when ``stdout`` is not a JSON object; the caller decides
    whether that is plain context, ignorable, or (``onFailure: block``) a block.
    """
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("hook output is not a JSON object")
    return _hook_output_from_dict(event, data)


def _cap_utf16(text: str, units: int) -> str:
    raw = text.encode("utf-16-le")
    if len(raw) <= units * 2:
        return text
    return raw[: units * 2].decode("utf-16-le", errors="ignore")


def merge(results: list[HookExecResult]) -> AggregatedHookResult:
    """04 §4.7 ``merge``: the first deny fixes the decision (later hooks cannot
    soften it), ``ask`` beats ``allow``, the last ``updatedInput`` wins (ignored
    once denied). Contexts, notices and messages from every hook are kept."""
    agg = AggregatedHookResult(results=list(results))
    cc: list[str] = []
    for r in results:
        if r.outcome == "block":
            agg.blocking_errors.append(r.feedback or "Blocked by hook.")
        elif r.outcome == "notice" and r.feedback:
            agg.user_notices.append(r.feedback)
        if r.additional_context:
            agg.additional_contexts.append(r.additional_context)
        if r.system_message:
            agg.system_messages.append(r.system_message)
        if not r.continue_:
            agg.prevent_continuation = True
            agg.stop_reason = agg.stop_reason or r.stop_reason
        if r.updated_tool_output is not None:
            agg.updated_tool_output = r.updated_tool_output
        if r.classifier_context:
            cc.append(r.classifier_context)
        if agg.decision == "deny":
            continue
        if _DECISION_RANK.get(r.decision, 0) > _DECISION_RANK.get(agg.decision, 0):
            agg.decision = r.decision
            agg.decision_reason = r.decision_reason
            agg.hook_name = r.hook_name
        if r.updated_input is not None and agg.decision != "deny":
            agg.updated_input = r.updated_input
    if agg.decision == "deny":
        agg.updated_input = None
    agg.classifier_context = _cap_utf16("\n".join(cc), CLASSIFIER_CONTEXT_CAP)
    return agg


# ------------------------------------------------------------------ helpers


def _hook_name(spec: HookSpec) -> str:
    if spec.type == "command":
        return spec.command or "command"
    if spec.type in ("prompt", "agent"):
        return f"{spec.type}: {(spec.prompt or '')[:40]}"
    if spec.type == "http":
        return spec.url or "http"
    if spec.type == "mcp_tool":
        return f"mcp__{spec.server}__{spec.tool}"
    return getattr(spec.callback, "__name__", "callback")


def _timeout_for(spec: HookSpec, event: HookEvent, env: dict[str, str]) -> float:
    if spec.timeout is not None:
        return float(spec.timeout)
    if event == HookEvent.SESSION_END:
        ms = env_int(env, "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS", int(SESSION_END_TIMEOUT_S * 1000), lo=1)
        return min(ms / 1000.0, SESSION_END_TIMEOUT_MAX_S)
    return DEFAULT_TIMEOUT_S.get(spec.type, 600.0)


def _fail_closed(spec: HookSpec, event: HookEvent) -> bool:
    return spec.on_failure == "block" and event in BLOCKING_CAPABLE_EVENTS


def _timeout_result(spec: HookSpec, event: HookEvent, timeout: float) -> HookExecResult:
    """Timed-out output is discarded (R36); fail-closed configurations block."""
    if (spec.timeout_fails_closed and event in TIMEOUT_FAIL_CLOSED_EVENTS) or _fail_closed(spec, event):
        return HookExecResult("block", f"Hook timed out after {timeout:g}s and is configured to fail closed.")
    return HookExecResult("ignore", f"Hook timed out after {timeout:g}s.")


def _interpret(spec: HookSpec, event: HookEvent, code: int, stdout: str, stderr: str) -> HookExecResult:
    """Exit-code semantics (04 R33): 0 = success (JSON or plain context), 2 =
    block with stderr to the model, anything else = notice to the user only;
    ``onFailure: block`` turns failures into blocks for blocking-capable events."""
    if code == 0:
        if not stdout.strip():
            return HookExecResult("ok")
        try:
            return parse_hook_stdout(event, stdout)
        except ValueError:
            if event in PLAIN_STDOUT_CONTEXT_EVENTS:
                return HookExecResult("ok", additional_context=stdout.strip())
            if _fail_closed(spec, event):
                return HookExecResult("block", "Hook printed output that is not valid JSON.")
            return HookExecResult("ok")
    if code == 2:
        return HookExecResult("block", stderr.strip() or "Blocked by hook.")
    msg = stderr.strip() or f"Hook exited with status {code}."
    if _fail_closed(spec, event):
        return HookExecResult("block", msg)
    return HookExecResult("notice", msg)


def _run_with_timeout(fn: Callable[[], HookExecResult], timeout: float) -> HookExecResult | None:
    """Run ``fn`` in a daemon thread; ``None`` on timeout (the thread is abandoned)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported as a hook failure
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    if "e" in box:
        raise box["e"]
    return box["r"]


def _expand_arg(arg: str, env: dict[str, str]) -> str:
    """Only the three documented variables are expanded in exec-form args (R38)."""
    for name in EXPANDABLE_ARG_VARS:
        val = env.get(name)
        if val is not None:
            arg = arg.replace("${" + name + "}", val).replace("$" + name, val)
    return arg


def _interpolate_tool_input(value: Any, tool_input: dict[str, Any]) -> Any:
    """``${tool_input.<field>}`` interpolation for ``mcp_tool`` hooks (04 §2.6)."""
    if isinstance(value, str):
        return re.sub(r"\$\{tool_input\.([A-Za-z0-9_]+)\}", lambda m: str(tool_input.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {k: _interpolate_tool_input(v, tool_input) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_tool_input(v, tool_input) for v in value]
    return value


def _render_items(items: list[Any]) -> str:
    lines: list[str] = []
    for it in items:
        if isinstance(it, Message) and it.role in ("user", "assistant") and not it.is_meta:
            text = it.text()
            if text:
                lines.append(f"{it.role}: {text}")
    return "\n".join(lines)


# ------------------------------------------------------------------ runner


class HookRunner:
    """``interfaces.HookRunner``. ``config`` may be given directly (tests / SDK hosts)."""

    def __init__(self, config: dict[HookEvent, list[HookMatcherGroup]] | None = None, *, settings: Settings | None = None) -> None:
        self.config = config or {}
        self.settings = settings
        if settings is not None:
            for ev, groups in load_hook_config(settings).items():
                self.config.setdefault(ev, []).extend(groups)
        self._once_fired: set[Any] = set()
        self._lock = threading.Lock()

    def register(self, event: HookEvent, group: HookMatcherGroup) -> None:
        self.config.setdefault(event, []).append(group)

    def register_callback(
        self, event: HookEvent, fn: Callable[[dict[str, Any]], Any], *, matcher: str | None = None, timeout: float | None = None
    ) -> HookSpec:
        """07 R35 convenience: a host ``hook_callback`` id becomes a callback hook."""
        spec = HookSpec("callback", callback=fn, timeout=timeout)
        self.register(event, HookMatcherGroup(matcher, [spec]))
        return spec

    # ---- selection
    def select(self, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> list[HookSpec]:
        """Matcher -> managed-only -> ``if`` filter -> ``once`` -> dedupe (04 §4.7)."""
        settings = self.settings or ctx.settings
        if settings.get("disableAllHooks", False):
            return []
        managed_only = bool(settings.get("allowManagedHooksOnly", False))
        values = event_match_values(event, payload)
        specs: list[HookSpec] = []
        seen: set[Any] = set()
        for group in self.config.get(event, []):
            if event in MATCH_FIELD and not matcher_matches(group.matcher, values, event=event):
                continue
            for h in group.hooks:
                if managed_only and not h.managed:
                    continue
                if h.if_ and not if_filter_matches(h.if_, payload, cwd=str(ctx.cwd)):
                    continue
                key = h.dedupe_key()
                if key in seen or (h.once and key in self._once_fired):
                    continue
                seen.add(key)
                specs.append(h)
        return specs

    def run(self, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> AggregatedHookResult:
        specs = self.select(event, payload, ctx)
        if not specs:
            return AggregatedHookResult()
        with self._lock:
            for h in specs:
                if h.once:
                    self._once_fired.add(h.dedupe_key())
        # every hook sees its own copy of the *same original* input (R34)
        with ThreadPoolExecutor(max_workers=min(len(specs), 16)) as pool:
            futures = [pool.submit(self.run_one, h, event, copy.deepcopy(payload), ctx) for h in specs]
            results = [f.result() for f in futures]
        agg = merge(results)
        if event == HookEvent.POST_TOOL_USE and agg.classifier_context:
            ctx.extra.setdefault("classifier_context", []).append(agg.classifier_context)
        return agg

    def run_one(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> HookExecResult:
        if spec.is_async or spec.async_rewake:
            threading.Thread(target=self._run_async, args=(spec, event, payload, ctx), daemon=True).start()
            return HookExecResult("ok", specific={"async": True}, hook_name=_hook_name(spec))
        r = self._execute(spec, event, payload, ctx)
        r.hook_name = r.hook_name or _hook_name(spec)
        return r

    def _run_async(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> None:
        r = self._execute(spec, event, payload, ctx)
        if spec.async_rewake and r.outcome == "block" and ctx.notifications is not None:
            from harnesslab.agents.notifications import notification_message

            ctx.notifications.push(notification_message(f"A background hook ({_hook_name(spec)}) reported:\n{r.feedback}"))

    def _execute(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> HookExecResult:
        env = {**os.environ, **dict(ctx.env)}
        timeout = _timeout_for(spec, event, env)
        try:
            if spec.type == "command":
                return self._run_command(spec, event, payload, ctx, timeout)
            runners: dict[str, Callable[[], HookExecResult]] = {
                "callback": lambda: self._run_callback(spec, event, payload),
                "prompt": lambda: self._run_prompt(spec, event, payload, ctx),
                "agent": lambda: self._run_prompt(spec, event, payload, ctx),
                "http": lambda: self._run_http(spec, event, payload, ctx, timeout),
                "mcp_tool": lambda: self._run_mcp_tool(spec, event, payload, ctx),
            }
            fn = runners.get(spec.type)
            if fn is None:
                return _interpret(spec, event, 1, "", f"unknown hook type {spec.type}")
            r = _run_with_timeout(fn, timeout)
            return _timeout_result(spec, event, timeout) if r is None else r
        except Exception as e:  # spawn failure etc. -> exit-code-1 semantics
            log.warning("hook %s failed: %s", _hook_name(spec), e)
            return _interpret(spec, event, 1, "", f"Hook failed: {e}")

    # ---- hook types
    def _run_command(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext, timeout: float) -> HookExecResult:
        env = {**os.environ, **dict(ctx.env)}
        env["CLAUDE_PROJECT_DIR"] = str(ctx.original_cwd or ctx.cwd)
        if ctx.effort:
            env["CLAUDE_EFFORT"] = str(ctx.effort)
        if event in ENV_FILE_EVENTS:
            env_dir = ctx.session_state_dir / "hook-env"
            env_dir.mkdir(parents=True, exist_ok=True)
            env_file = env_dir / f"{event.value}.sh"
            env["CLAUDE_ENV_FILE"] = str(env_file)
            files = ctx.extra.setdefault("hook_env_files", [])
            if str(env_file) not in files:
                files.append(str(env_file))
        if spec.args is not None:
            argv = [_expand_arg(spec.command or "", env)] + [_expand_arg(a, env) for a in spec.args]
        elif spec.shell == "powershell":
            argv = ["pwsh", "-NoProfile", "-Command", spec.command or ""]
        else:
            argv = [shutil.which("bash") or "/bin/sh", "-c", spec.command or ""]
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(ctx.cwd),
                env=env,
            )
        except subprocess.TimeoutExpired:
            return _timeout_result(spec, event, timeout)
        except OSError as e:
            return _interpret(spec, event, 1, "", f"Could not start hook: {e}")
        return _interpret(spec, event, proc.returncode, proc.stdout, proc.stderr)

    def _run_callback(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any]) -> HookExecResult:
        assert spec.callback is not None, "callback hook without a function"
        out = spec.callback(payload)
        if out is None:
            return HookExecResult("ok")
        if isinstance(out, HookExecResult):
            return out
        if isinstance(out, dict):
            return _hook_output_from_dict(event, out)
        return _interpret(spec, event, 0, str(out), "")

    def _run_prompt(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> HookExecResult:
        """LLM-evaluated hook (R37): structured ``{ok, reason, impossible?}``,
        thinking off; on context overflow retry with half the transcript."""
        if ctx.model is None:
            return _interpret(spec, event, 1, "", "No model available for prompt hook.")
        data = dict(payload)
        if event in (HookEvent.STOP, HookEvent.SUBAGENT_STOP) and "transcript" not in data:
            data["transcript"] = _render_items(ctx.extra.get("conversation_items", []) or [])
        for _attempt in range(4):
            args = json.dumps(data, default=str)
            prompt = spec.prompt or ""
            text = prompt.replace("$ARGUMENTS", args) if "$ARGUMENTS" in prompt else f"{prompt}\n\nEvent data:\n{args}"
            req = ModelRequest(
                model=spec.model or ctx.model_id,
                system=[SystemBlock(PROMPT_HOOK_SYSTEM)],
                messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
                max_tokens=1024,
                thinking={"type": "disabled"},
                query_source="hook",
            )
            try:
                acc = StreamAccumulator()
                for ev in ctx.model.stream(req, ctx.abort.signal):
                    acc.feed(ev)
                reply = acc.message().text()
            except APIError as e:
                if e.kind in (ErrorKind.PROMPT_TOO_LONG, ErrorKind.CONTEXT_OVERFLOW):
                    t = data.get("transcript")
                    if isinstance(t, str) and len(t) > 1:
                        data["transcript"] = t[len(t) // 2 :]
                    elif isinstance(t, list) and len(t) > 1:
                        data["transcript"] = t[len(t) // 2 :]
                    else:
                        return _interpret(spec, event, 1, "", "Prompt hook input too long.")
                    continue
                raise
            m = re.search(r"\{.*\}", reply, re.DOTALL)
            try:
                verdict = json.loads(m.group(0)) if m else None
            except ValueError:
                verdict = None
            if not isinstance(verdict, dict) or not isinstance(verdict.get("ok"), bool):
                return _interpret(spec, event, 1, "", "Prompt hook returned no valid verdict.")
            reason = str(verdict.get("reason", ""))
            if verdict["ok"]:
                return HookExecResult("ok", decision_reason=reason)
            if verdict.get("impossible") is True:
                return HookExecResult("ok", system_message=reason, specific={"impossible": True})
            r = HookExecResult("block", reason or "Condition not met.")
            if event == HookEvent.PRE_TOOL_USE:
                r.decision, r.decision_reason = "deny", r.feedback
            return r
        return _interpret(spec, event, 1, "", "Prompt hook input too long.")

    def _run_http(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext, timeout: float) -> HookExecResult:
        """POST the payload; only to ``allowedHttpHookUrls`` when that list is set
        (R39); ``$VAR`` in headers expands only for ``allowedEnvVars``."""
        settings = self.settings or ctx.settings
        url = spec.url or ""
        allowed = settings.get("allowedHttpHookUrls")
        if allowed is not None and not any(fnmatch.fnmatchcase(url, p) for p in allowed):
            return _interpret(spec, event, 1, "", f"HTTP hook URL {url} is not in allowedHttpHookUrls.")
        env = {**os.environ, **dict(ctx.env)}

        def expand(v: str) -> str:
            return re.sub(
                r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                lambda m: env.get(m.group(1), "") if m.group(1) in spec.allowed_env_vars else "",
                v,
            )

        headers = {"Content-Type": "application/json", **{k: expand(v) for k, v in spec.headers.items()}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - allowlisted above
                body = resp.read().decode("utf-8", errors="replace")
                return _interpret(spec, event, 0, body, "")
        except urllib.error.HTTPError as e:
            return _interpret(spec, event, 1, "", f"HTTP hook returned {e.code}.")

    def _run_mcp_tool(self, spec: HookSpec, event: HookEvent, payload: dict[str, Any], ctx: SessionContext) -> HookExecResult:
        tool_input = payload.get("tool_input") or {}
        args = _interpolate_tool_input(spec.tool_input, tool_input if isinstance(tool_input, dict) else {})
        client = next((c for c in ctx.mcp_clients if getattr(c, "name", None) == spec.server), None)
        if client is None or not hasattr(client, "call_tool"):
            return _interpret(spec, event, 1, "", f"MCP server {spec.server} is not connected.")
        out = client.call_tool(spec.tool, args)
        return _interpret(spec, event, 0, out if isinstance(out, str) else json.dumps(out, default=str), "")
