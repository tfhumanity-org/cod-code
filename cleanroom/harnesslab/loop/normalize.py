"""Normalization and tool-pairing repair before every request (WP1).

Implements spec 01 R55 (merge consecutive user rows, fold reminders, drop empty
text, placeholder for empty messages, strip lone surrogates, strip thinking per
plan), 01 R56 (pairing repair with explanatory placeholders; strict mode
raises), 01 R57 (media cap: oldest images/documents replaced by a marker),
02 §2.3 / R30 (attachments rendered to ``<system-reminder>`` meta user text at
serialization time via ``types.render_attachment_text``). Takeaway 01#T5, P04.

Pipeline (``to_api_messages``)::

    items after the last compact boundary
      -> render attachments, demote reminder system rows, drop other system rows
         and withheld API-error rows
      -> wrap meta user text in <system-reminder> (02 R30)
      -> repair tool pairing (01 R56)
      -> merge consecutive same-role rows; tool_result blocks first (01 R55)
      -> clean text / strip thinking / empty-message placeholder (01 R55)
      -> cap media (01 R57)
      -> API dicts (fresh dicts: stamping cache markers never mutates items)

Design choices (spec silent):

* The thinking "strip plan" keeps signed thinking blocks and drops unsigned ones
  (an unsigned block cannot be verified by the server, e.g. it came from
  another model or a fake). ``api.retry`` strips *all* thinking on an
  invalid-signature rejection (01 R25).
* Mid-conversation system reminders (01 R46) are always sent user-side: a
  ``system`` row with ``subtype="reminder"`` is demoted to a meta user row.
* Server / MCP-server tool blocks are not modelled, so "drop orphan server tool
  uses" has nothing to act on (Deferred).
* Media cap: ``MAX_MEDIA_BLOCKS`` blocks and ``MAX_MEDIA_BYTES`` base64 bytes.

Nothing here mutates the caller's items: messages are copied before editing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any

from harnesslab.tokens import is_compact_boundary
from harnesslab.types import (
    SYNTHETIC_RESULT_TEXT,
    Attachment,
    ContentBlock,
    ConversationItem,
    DocumentBlock,
    ImageBlock,
    Message,
    SyntheticReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    render_attachment_text,
)
from harnesslab.util.text import REMINDER_OPEN, wrap_system_reminder

MAX_MEDIA_BLOCKS = 100  # design choice; spec names a cap but not its value (01 R57)
MAX_MEDIA_BYTES = 32 * 1024 * 1024  # design choice (01 R57 "byte cap")
EMPTY_PLACEHOLDER = "(no content)"
MEDIA_REMOVED_TEXT = "[image or document removed due to request size limit]"
ORPHAN_REMOVED_TEXT = "[a tool result whose tool call is no longer in the conversation was removed]"

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


class PairingError(Exception):
    """Raised in strict mode instead of repairing (01 R56)."""


@dataclass
class RepairReport:
    """Each repair is logged with a structural fingerprint (01 R56)."""

    actions: list[str] = field(default_factory=list)
    fingerprint: str = ""


# ------------------------------------------------------------------ helpers


def _copy(m: Message) -> Message:
    """Shallow copy with an independent content list (blocks themselves are reused
    unless edited, in which case they are replaced, never mutated)."""
    return replace(m, content=list(m.content))


def fingerprint(messages: list[Message]) -> str:
    """Structure-only fingerprint: roles and block kinds, no text (01 R56 logging)."""
    shape = "|".join(m.role[0] + ":" + ",".join(b.type for b in m.content) for m in messages)
    return hashlib.sha1(shape.encode()).hexdigest()[:12]


def strip_lone_surrogates(text: str) -> str:
    """Remove unpaired UTF-16 surrogates; they make the request body invalid JSON (01 R55)."""
    return _LONE_SURROGATE.sub("", text)


def as_reminder(text: str) -> str:
    """Wrap in ``<system-reminder>`` unless the text already is one (02 R30).

    Integration note: the attachment renderers registered by
    ``context.attachments`` return text that is *already* wrapped, while the
    loop's own renderers (mid-turn input, max-turns) return bare text. Wrapping
    only once keeps the envelope single either way."""
    return text if text.lstrip().startswith(REMINDER_OPEN) else wrap_system_reminder(text)


def ensure_renderers_registered() -> None:
    """Importing ``context.attachments`` registers its renderers in
    ``types.ATTACHMENT_RENDERERS``; do it lazily so this module keeps no
    import-time dependency on WP4 (ARCHITECTURE §4 import rule)."""
    try:
        import harnesslab.context.attachments  # noqa: F401
    except Exception:  # pragma: no cover - a broken renderer module must not break requests
        pass


# ------------------------------------------------------------------ stages


def render_attachments(items: list[ConversationItem]) -> list[Message]:
    """Attachment -> meta user Message wrapped in system-reminder (02 R30).

    Also: system ``reminder`` rows are demoted to user reminders (01 R46), other
    system rows (boundaries, notices) and withheld API-error rows are dropped,
    and text of every meta user row is wrapped in reminder tags (02 R30).
    """
    ensure_renderers_registered()
    out: list[Message] = []
    for it in items:
        if isinstance(it, Attachment):
            text = render_attachment_text(it)
            if text:
                out.append(Message("user", [TextBlock(as_reminder(text))], uuid=it.uuid, is_meta=True))
            continue
        if it.role == "system":
            if it.subtype == "reminder":
                out.append(Message("user", [TextBlock(as_reminder(it.text()))], uuid=it.uuid, is_meta=True))
            continue
        if it.is_api_error:
            continue
        if it.role == "user" and it.is_meta:
            m = _copy(it)
            m.content = [
                TextBlock(wrap_system_reminder(b.text))
                if isinstance(b, TextBlock) and not b.text.lstrip().startswith(REMINDER_OPEN)
                else b
                for b in m.content
            ]
            out.append(m)
            continue
        out.append(it)
    return out


def repair_tool_pairing(
    messages: list[Message], *, strict: bool = False, resumed: bool = False
) -> tuple[list[Message], RepairReport]:
    """01 R56: dedupe tool_use ids; drop orphan results; add an error result for
    every unanswered tool_use (``SyntheticReason.MISSING``); on resume replace a
    leading orphan with a removal note.

    "Answered" means: a ``tool_result`` for the id appears in the run of user
    messages that directly follows the assistant row (the API requirement).
    Results anywhere else are misplaced and removed.
    """
    report = RepairReport()

    def note(action: str) -> None:
        if strict:
            raise PairingError(f"tool pairing needs repair: {action}")
        report.actions.append(action)

    msgs = [_copy(m) for m in messages]

    # 1. dedupe tool_use ids across the conversation (first occurrence wins)
    seen_use: set[str] = set()
    for m in msgs:
        if m.role != "assistant":
            continue
        kept: list[ContentBlock] = []
        for b in m.content:
            if isinstance(b, ToolUseBlock):
                if b.id in seen_use:
                    note(f"dropped duplicate tool_use {b.id}")
                    continue
                seen_use.add(b.id)
            kept.append(b)
        m.content = kept

    # 2. walk: for each assistant row, the following user run must answer every tool_use
    out: list[Message] = []
    i = 0
    expected: set[str] = set()  # ids the current user run may answer
    answered: set[str] = set()
    pending_assistant: Message | None = None
    pending_idx = -1

    def close_run() -> None:
        """Synthesise MISSING results for tool uses the finished run did not answer."""
        nonlocal pending_assistant, pending_idx
        if pending_assistant is None:
            return
        missing = [b for b in pending_assistant.tool_uses() if b.id not in answered]
        if missing:
            for b in missing:
                note(f"added missing result for {b.id}")
            synth = [ToolResultBlock(b.id, SYNTHETIC_RESULT_TEXT[SyntheticReason.MISSING], True) for b in missing]
            # insert into the first user row after the assistant, or a fresh one
            idx = pending_idx + 1
            if idx < len(out) and out[idx].role == "user":
                first = out[idx]
                n_results = sum(isinstance(b, ToolResultBlock) for b in first.content)
                first.content = first.content[:n_results] + synth + first.content[n_results:]
            else:
                out.insert(idx, Message("user", synth, is_meta=False))
        pending_assistant = None

    while i < len(msgs):
        m = msgs[i]
        if m.role == "assistant":
            close_run()
            expected = {b.id for b in m.tool_uses()}
            answered = set()
            pending_assistant = m if expected else None
            pending_idx = len(out)
            out.append(m)
        else:  # user
            kept = []
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    if b.tool_use_id in expected and b.tool_use_id not in answered:
                        answered.add(b.tool_use_id)
                        kept.append(b)
                        continue
                    if resumed and not out and not kept:
                        note(f"replaced leading orphan result {b.tool_use_id}")
                        kept.append(TextBlock(ORPHAN_REMOVED_TEXT))
                        continue
                    note(f"dropped orphan/misplaced result {b.tool_use_id}")
                    continue
                kept.append(b)
            if kept:  # a row left empty by dropped results disappears
                m.content = kept
                out.append(m)
        i += 1
    close_run()
    if report.actions:
        report.fingerprint = fingerprint(messages)
    return out, report


def merge_consecutive_user(messages: list[Message]) -> list[Message]:
    """Merge adjacent rows of the same role (01 R55). For user rows the
    ``tool_result`` blocks are moved to the front, as the API requires results
    to open the user turn that follows the tool calls; reminder text is thereby
    folded in *after* the results."""
    out: list[Message] = []
    for m in messages:
        if out and out[-1].role == m.role:
            prev = out[-1]
            merged = _copy(prev)
            merged.content = prev.content + m.content
            merged.is_meta = prev.is_meta and m.is_meta
            merged.extra = {**prev.extra, **m.extra}
            out[-1] = merged
        else:
            out.append(m)
    for i, m in enumerate(out):
        if m.role == "user" and any(isinstance(b, ToolResultBlock) for b in m.content):
            results = [b for b in m.content if isinstance(b, ToolResultBlock)]
            rest = [b for b in m.content if not isinstance(b, ToolResultBlock)]
            if m.content != results + rest:
                out[i] = replace(m, content=results + rest)
    return out


def clean_message(m: Message) -> Message:
    """01 R55: strip lone surrogates, drop empty text, strip unsigned thinking,
    placeholder for a message that ended up empty."""
    blocks: list[ContentBlock] = []
    for b in m.content:
        if isinstance(b, TextBlock):
            text = strip_lone_surrogates(b.text)
            if not text.strip():
                continue
            blocks.append(b if text == b.text else TextBlock(text, b.cache_control))
        elif isinstance(b, ThinkingBlock):
            if b.signature:  # strip plan: unsigned thinking cannot be verified
                blocks.append(b)
        elif isinstance(b, ToolResultBlock) and isinstance(b.content, str):
            text = strip_lone_surrogates(b.content)
            blocks.append(b if text == b.content else replace(b, content=text))
        else:
            blocks.append(b)
    if not blocks:
        blocks = [TextBlock(EMPTY_PLACEHOLDER)]
    return replace(m, content=blocks)


def _media_size(b: ContentBlock) -> int:
    return len(b.data) if isinstance(b, (ImageBlock, DocumentBlock)) else 0


def cap_media(
    messages: list[Message], max_blocks: int = MAX_MEDIA_BLOCKS, *, max_bytes: int = MAX_MEDIA_BYTES
) -> list[Message]:
    """01 R57: replace the *oldest* media blocks with a marker until both caps hold.
    Media nested inside tool results count too."""
    # positions in conversation order: (msg index, block index, nested index | None)
    positions: list[tuple[int, int, int | None, int]] = []
    for mi, m in enumerate(messages):
        for bi, b in enumerate(m.content):
            if isinstance(b, (ImageBlock, DocumentBlock)):
                positions.append((mi, bi, None, _media_size(b)))
            elif isinstance(b, ToolResultBlock) and not isinstance(b.content, str):
                for ni, nb in enumerate(b.content):
                    if isinstance(nb, (ImageBlock, DocumentBlock)):
                        positions.append((mi, bi, ni, _media_size(nb)))
    count, total = len(positions), sum(p[3] for p in positions)
    drop: list[tuple[int, int, int | None]] = []
    for mi, bi, ni, size in positions:
        if count <= max_blocks and total <= max_bytes:
            break
        drop.append((mi, bi, ni))
        count -= 1
        total -= size
    if not drop:
        return messages
    out = [_copy(m) for m in messages]
    marker = TextBlock(MEDIA_REMOVED_TEXT)
    for mi, bi, ni in drop:
        if ni is None:
            out[mi].content[bi] = marker
        else:
            tr = out[mi].content[bi]
            assert isinstance(tr, ToolResultBlock) and not isinstance(tr.content, str)
            nested = list(tr.content)
            nested[ni] = marker
            out[mi].content[bi] = replace(tr, content=nested)
    return out


def after_last_boundary(items: list[ConversationItem]) -> list[ConversationItem]:
    """Only rows after the newest compact boundary are sent (05 R1 anchor rule)."""
    for i in range(len(items) - 1, -1, -1):
        if is_compact_boundary(items[i]):
            return items[i + 1 :]
    return items


def to_api_messages(
    items: list[ConversationItem], *, strict: bool = False, resumed: bool = False
) -> tuple[list[dict[str, Any]], RepairReport]:
    """Full pipeline: drop system rows, render attachments, repair pairing, merge,
    clean text, cap media -> API-shaped messages."""
    msgs = render_attachments(after_last_boundary(items))
    msgs, report = repair_tool_pairing(msgs, strict=strict, resumed=resumed)
    msgs = merge_consecutive_user(msgs)
    msgs = [clean_message(m) for m in msgs]
    msgs = cap_media(msgs)
    api: list[dict[str, Any]] = []
    for m in msgs:
        d = m.to_api()
        if m.role == "user" and m.is_meta and m.extra.get("request_scoped"):
            d["_ephemeral"] = True  # consumed by api.caching, removed before sending
        api.append(d)
    return api, report
