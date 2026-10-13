"""Deterministic, budgeted re-injection after compaction (WP4).

Implements spec 05 §4.5 and R21: up to 5 most recently read files (excluding
files already in kept messages, memory and plan files) re-read from disk at
<= 5k tokens each, 50k total; invoked skills newest-first at <= 5k each, 25k
total (truncated with a note pointing to the skill file); plan file reference
+ plan-mode reminder; background task / async shell status; MCP instructions;
deferred-tool listing; active goal. Planning budget ~70k. Also 02 R32
``invoked_skills`` (re-inject bodies with "do not re-run"). Takeaway 05#T3,
08#T6 (durable state re-injected after compaction).

Inputs are duck-typed so this module works before other WPs land:

* ``read_snapshot``: ``[(path, entry)]`` oldest -> newest (the ledger's LRU
  order); ``entry`` may be anything (only the path is used: files are re-read
  from disk so the model sees current content).
* ``ctx.invoked_skills``: dicts or objects with ``name`` and ``body``/``content``
  and optionally ``path``; oldest -> newest.
* ``ctx.mcp_clients``: objects with ``name`` and ``instructions``.
* ``ctx.extra["active_goal"]``: free text.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harnesslab.permissions.types import PermissionMode
from harnesslab.runtime import SessionContext
from harnesslab.tokens import CHARS_PER_TOKEN, estimate_text_tokens
from harnesslab.types import Attachment, ConversationItem, Message, ToolUseBlock

MAX_FILES = 5
FILE_TOKENS = 5_000
FILES_TOTAL_TOKENS = 50_000
SKILL_TOKENS = 5_000
SKILLS_TOTAL_TOKENS = 25_000
POST_COMPACT_BUDGET = 70_000  # 20k summary + 5 x 5k files + 25k skills (05 §4.5)


def _truncate(text: str, max_tokens: int, note: str) -> tuple[str, bool]:
    if len(text) <= max_tokens * CHARS_PER_TOKEN:
        return text, False
    limit = max_tokens * CHARS_PER_TOKEN - len(note) - 8  # the note counts against the cap
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return f"{cut}\n\n[{note}]", True


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _paths_in(kept: list[ConversationItem]) -> set[str]:
    """Paths already visible in kept messages (tool inputs naming a file)."""
    out: set[str] = set()
    for it in kept:
        if isinstance(it, Message):
            for b in it.content:
                if isinstance(b, ToolUseBlock):
                    for key in ("file_path", "path", "notebook_path"):
                        v = b.input.get(key)
                        if isinstance(v, str):
                            out.add(os.path.abspath(v))
    return out


def _is_memory_file(ctx: SessionContext, path: str) -> bool:
    from harnesslab.context.memory import memory_dir  # own WP, local to keep import graph flat

    try:
        mem = str(memory_dir(ctx).resolve())
    except Exception:
        return False
    rp = os.path.realpath(path)
    return rp == mem or rp.startswith(mem + os.sep) or os.path.basename(path) in ("CLAUDE.md", "CLAUDE.local.md")


def file_attachments(ctx: SessionContext, read_snapshot: list[tuple[str, Any]], kept: list[ConversationItem]) -> list[Attachment]:
    skip = _paths_in(kept)
    plan = str(ctx.plan_file.resolve()) if ctx.plan_file else None
    out: list[Attachment] = []
    total = 0
    seen: set[str] = set()
    for path, _entry in reversed(read_snapshot):  # most recent first
        ap = os.path.abspath(path)
        if ap in seen or ap in skip or (plan and os.path.realpath(ap) == plan) or _is_memory_file(ctx, ap):
            continue
        seen.add(ap)
        if len(out) >= MAX_FILES:
            break
        try:
            text = Path(ap).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        body, truncated = _truncate(text, FILE_TOKENS, "File truncated here; use the Read tool with an offset for the rest")
        n = estimate_text_tokens(body)
        if total + n > FILES_TOTAL_TOKENS:
            break
        total += n
        out.append(Attachment("compact_file_reference", {"path": ap, "content": body, "truncated": truncated}))
    return out


def skill_attachment(ctx: SessionContext) -> list[Attachment]:
    skills: list[dict[str, Any]] = []
    total = 0
    for s in reversed(list(ctx.invoked_skills)):  # newest first
        name = _get(s, "name", default="skill")
        body = str(_get(s, "body", "content", default="") or "")
        path = _get(s, "path", default=None)
        note = f"Skill body truncated; read {path} for the rest" if path else "Skill body truncated; reload the skill for the rest"
        body, _ = _truncate(body, SKILL_TOKENS, note)
        n = estimate_text_tokens(body)
        if total + n > SKILLS_TOTAL_TOKENS:
            break
        total += n
        skills.append({"name": str(name), "body": body, "path": str(path) if path else None})
    return [Attachment("invoked_skills", {"skills": skills})] if skills else []


def build_reinjection(ctx: SessionContext, read_snapshot: list[tuple[str, Any]], kept: list[ConversationItem]) -> list[Attachment]:
    """05 §4.5 in order: files, skills, plan, plan mode, tasks, MCP, deferred tools, goal."""
    out = file_attachments(ctx, read_snapshot, kept)
    out += skill_attachment(ctx)
    if ctx.plan_file is not None and Path(ctx.plan_file).exists():
        try:
            content = Path(ctx.plan_file).read_text(encoding="utf-8")
        except OSError:
            content = ""
        out.append(Attachment("plan_file_reference", {"path": str(ctx.plan_file), "content": content}))
    if ctx.permissions.mode == PermissionMode.PLAN:
        out.append(Attachment("plan_mode", {"full": True, "variant": "reentry", "plan_file": str(ctx.plan_file or "")}))
    tasks = ctx.tasks
    if tasks is not None and hasattr(tasks, "list"):
        try:
            running = tasks.list(status="running")
        except Exception:
            running = []
        if running:
            out.append(
                Attachment(
                    "task_status",
                    {"tasks": [{"id": t.id, "kind": t.kind, "description": t.description, "status": t.status,
                                "output_file": str(t.output_file) if t.output_file else None} for t in running]},
                )
            )
    mcp = [(str(_get(c, "name", default="")), _get(c, "instructions", default=None)) for c in ctx.mcp_clients]
    mcp = [(n, i) for n, i in mcp if i]
    if mcp:
        out.append(Attachment("mcp_instructions", {"servers": [{"name": n, "instructions": i} for n, i in mcp]}))
    deferred = sorted(
        str(getattr(t, "name", "")) for t in ctx.tools if getattr(t, "should_defer", False) and getattr(t, "name", "") not in ctx.discovered_tools
    )
    if deferred:
        out.append(Attachment("deferred_tools_delta", {"added": deferred, "removed": []}))
    goal = ctx.extra.get("active_goal")
    if goal:
        out.append(Attachment("goal", {"goal": str(goal)}))
    return out
