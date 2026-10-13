"""Tiny demo REPL (WP1). Runs fully offline with a ScriptedModel by default.

Usage::

    python -m harnesslab.cli                 # offline demo agent (ScriptedModel)
    python -m harnesslab.cli --model claude-x  # real model via the anthropic adapter
    python -m harnesslab.cli --ndjson        # speak the host protocol on stdio (host.protocol)

The offline demo uses ``demo_model()``: a ScriptedModel whose turns are callables
that react to the last user message, e.g. ``read <path>`` -> Read tool call,
``run <cmd>`` -> Bash tool call, ``list <glob>`` -> Glob, anything else -> echo.
After a tool result it summarises the result in one line. This exercises the real
loop, executor, permissions (terminal y/n prompter) and transcript without a
network. Spec refs: 01 R59 / 07 R30 (host flags, ``--ndjson``), 01#T12.

Flags implemented (07 R30 subset): ``--ndjson``, ``--model``, ``--max-turns``,
``--permission-mode``; plus ``--cwd``, ``--config-dir``, ``--no-persist``.
``main`` accepts optional ``stdin`` / ``stdout`` streams for tests.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from harnesslab.interfaces import PermissionPrompter
from harnesslab.model.base import ModelRequest
from harnesslab.model.scripted import ScriptedModel, ScriptedTurn, call, say, turn
from harnesslab.permissions.types import PermissionAnswer, PermissionMode, PermissionPromptRequest, PermissionUpdate, Rule
from harnesslab.types import MessageEvent, SystemNoticeEvent, TurnEndEvent
from harnesslab.util.text import REMINDER_OPEN

RESULT_PREVIEW_CHARS = 800


def _last_user_turn(request: ModelRequest) -> tuple[str, str | None]:
    """``(typed_text, tool_result_text)`` of the last user message in the request."""
    for m in reversed(request.messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            parts = []
            for r in results:
                c = r.get("content")
                parts.append(c if isinstance(c, str) else "".join(x.get("text", "") for x in c or []))
            return "", "\n".join(parts)
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and not b.get("text", "").lstrip().startswith(REMINDER_OPEN)]
        if texts:
            return texts[-1].strip(), None
    return "", None


def demo_reply(request: ModelRequest) -> ScriptedTurn:
    """The demo "brain": map the last user message to a scripted turn."""
    text, result = _last_user_turn(request)
    if result is not None:
        lines = result.splitlines()
        first = lines[0][:200] if lines else "(empty)"
        return say(f"The tool returned {len(lines)} line(s); the first one is: {first}")
    verb, _, arg = text.partition(" ")
    arg = arg.strip()
    if verb == "read" and arg:
        return turn(f"Reading {arg}.", call("Read", {"file_path": str(Path(arg).expanduser().absolute())}))
    if verb == "run" and arg:
        return turn(f"Running `{arg}`.", call("Bash", {"command": arg, "description": "Demo command"}))
    if verb == "list" and arg:
        return turn(f"Listing files matching {arg}.", call("Glob", {"pattern": arg}))
    return say(f"(offline demo) You said: {text}" if text else "(offline demo) Say `read <path>`, `run <cmd>` or `list <glob>`.")


class _EndlessModel(ScriptedModel):
    """A ScriptedModel that never runs out: every request gets ``demo_reply``."""

    def stream(self, request: ModelRequest, abort: Any = None):  # type: ignore[override]
        if not self.remaining:
            self.add(demo_reply)
        return super().stream(request, abort)


def demo_model() -> ScriptedModel:
    """An endless ScriptedModel driven by callables (see module docstring)."""
    return _EndlessModel(name="offline-demo")


def terminal_prompter(inp: TextIO | None = None, out: TextIO | None = None) -> PermissionPrompter:
    """Ask y/n/always on stdin for residual permission asks (04 §2.1 default mode)."""

    def ask(req: PermissionPromptRequest) -> PermissionAnswer:
        i, o = inp or sys.stdin, out or sys.stdout
        why = f" ({req.decision_reason_text})" if req.decision_reason_text else ""
        what = req.description or str(req.input)
        default = "n" if req.default_to_no else "y"
        o.write(f"\nAllow {req.tool_name}: {what}{why}? [y]es / [n]o / [a]lways (default {default}): ")
        o.flush()
        answer = (i.readline() or "n").strip().lower() or default
        if answer in ("a", "always"):
            rule = Rule(req.tool_name, None, "allow")
            return PermissionAnswer(
                "allow",
                updated_permissions=[PermissionUpdate("addRules", "session", [rule], "allow")],
                decision_classification="user_permanent",
            )
        if answer in ("y", "yes"):
            return PermissionAnswer("allow", decision_classification="user_temporary")
        return PermissionAnswer("deny", message="The user declined.", decision_classification="user_reject")

    return ask


def build_engine(args: argparse.Namespace, prompter: PermissionPrompter | None) -> Any:
    """Create the model, context and ``session.Engine`` (separate so tests can swap it)."""
    from harnesslab.session import Engine, create_session_context

    if args.model:
        from harnesslab.model.anthropic_adapter import AnthropicModel

        model: Any = AnthropicModel(args.model)
    else:
        model = demo_model()
    ctx = create_session_context(
        model,
        cwd=Path(args.cwd).absolute(),
        config_dir=Path(args.config_dir).expanduser(),
        env=dict(os.environ),
        permission_mode=PermissionMode(args.permission_mode),
        prompter=prompter,
        persist=not args.no_persist,
    )
    ctx.max_turns = args.max_turns
    if args.ndjson:
        ctx.is_sdk = True
    return Engine(ctx)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="harnesslab", description="Educational coding-agent harness (offline demo by default).")
    p.add_argument("--ndjson", action="store_true", help="speak the NDJSON host protocol on stdin/stdout")
    p.add_argument("--model", default=None, help="real model id (needs the anthropic extra); default: offline demo")
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--permission-mode", default=PermissionMode.DEFAULT.value, choices=[m.value for m in PermissionMode])
    p.add_argument("--cwd", default=".")
    p.add_argument("--config-dir", default="~/.harnesslab")
    p.add_argument("--no-persist", action="store_true", help="do not write a transcript")
    return p.parse_args(argv)


def _print_event(ev: Any, out: TextIO) -> None:
    if isinstance(ev, MessageEvent):
        m = ev.message
        if m.role == "assistant":
            if m.text().strip():
                out.write(m.text().strip() + "\n")
            for b in m.tool_uses():
                out.write(f"  -> {b.name}({b.input})\n")
        elif m.role == "user":
            for r in m.tool_results():
                text = r.text()
                out.write("  <- " + (text[:RESULT_PREVIEW_CHARS] + (" [...]" if len(text) > RESULT_PREVIEW_CHARS else "")) + "\n")
    elif isinstance(ev, SystemNoticeEvent) and ev.text:
        out.write(f"[{ev.subtype}] {ev.text}\n")
    elif isinstance(ev, TurnEndEvent):
        out.write(f"[turn ended: {ev.reason.value}]\n")


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    args = parse_args(argv)
    inp, out = stdin or sys.stdin, stdout or sys.stdout
    if args.ndjson:
        from harnesslab.host.protocol import HostServer

        engine = build_engine(args, None)
        server = HostServer(engine, inp, out)
        engine.ctx.prompter = server.prompter
        return server.serve()

    engine = build_engine(args, terminal_prompter(inp, out))
    out.write("harnesslab demo - type a message, or `exit`.\n> ")
    out.flush()
    for line in inp:
        prompt = line.strip()
        if prompt in ("exit", "quit"):
            break
        if prompt:
            try:
                for ev in engine.submit(prompt):
                    _print_event(ev, out)
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                engine.interrupt()
        out.write("> ")
        out.flush()
    out.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
