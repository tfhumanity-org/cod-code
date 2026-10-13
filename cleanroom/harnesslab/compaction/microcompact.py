"""Lossless client-side tool-result clearing ("micro-compaction") (WP4).

Implements spec 05 R33 (replace old clearable tool results with a stub, keep
the most recent 5, act only when >= 20k tokens are saved; each result written to
disk first and the stub gives the path and says to use the Read tool), R34
(every replacement logged as a ``content-replacement`` metadata row and
replayed on resume for byte-identical requests), R32 (server-side clearing edit
payload builder for idle > 3900 s), R31 (keep-thinking edit); takeaway 05#T4;
P21 layer L2.

``DISABLE_MICROCOMPACT`` turns it off (00 R4 - desktop hosts disable it).

Deferred: R35 context-hint retry path (optional in the spec; needs a server that
answers 422/424).

Design choice: the ``context_management`` edit payload uses this library's own
field names (the spec gives semantics, not the wire format); a real adapter maps
them onto whatever the API version expects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from harnesslab.config import env_truthy
from harnesslab.runtime import SessionContext
from harnesslab.tokens import estimate_text_tokens
from harnesslab.types import ConversationItem, Message, TextBlock, ToolResultBlock

KEEP_RECENT = 5
MIN_SAVINGS_TOKENS = 20_000
IDLE_CLEAR_AFTER_S = 3900
IDLE_CLEAR_TRIGGER_TOOL_USES = 3
CLEARABLE_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "Edit", "Write"})
STUB_PREFIX = "[Earlier tool output cleared to save context."


@dataclass
class Replacement:
    tool_use_id: str
    stub: str
    path: str
    saved_tokens: int

    def to_row_value(self) -> dict[str, Any]:
        return {"toolUseId": self.tool_use_id, "stub": self.stub, "path": self.path}


def stub_text(path: str) -> str:
    return (
        f"{STUB_PREFIX} The full output was saved to {path}. "
        "Use the Read tool on that file if you need it again.]"
    )


def _result_text(b: ToolResultBlock) -> str | None:
    """Text content of a result, or None when it holds non-text media (not
    clearable losslessly as a text file)."""
    if isinstance(b.content, str):
        return b.content
    if all(isinstance(x, TextBlock) for x in b.content):
        return "".join(x.text for x in b.content)  # type: ignore[union-attr]
    return None


def microcompact(
    items: list[ConversationItem], ctx: SessionContext, *, log: bool = True
) -> tuple[list[ConversationItem], list[Replacement]]:
    """Clear old clearable tool results (05 R33). Returns new items (inputs are not
    mutated) and the replacements made; a no-op returns the same list and ``[]``.
    ``log=False`` lets a caller try the result before committing it (then call
    ``log_replacements``)."""
    if env_truthy(ctx.env, "DISABLE_MICROCOMPACT"):
        return items, []
    tool_names: dict[str, str] = {}
    for it in items:
        if isinstance(it, Message) and it.role == "assistant":
            for tu in it.tool_uses():
                tool_names[tu.id] = tu.name
    # candidates in conversation order: (item index, block index, block, text)
    cands: list[tuple[int, int, ToolResultBlock, str]] = []
    for i, it in enumerate(items):
        if not (isinstance(it, Message) and it.role == "user"):
            continue
        for j, b in enumerate(it.content):
            if isinstance(b, ToolResultBlock) and tool_names.get(b.tool_use_id) in CLEARABLE_TOOLS:
                text = _result_text(b)
                if text is not None and not text.startswith(STUB_PREFIX):
                    cands.append((i, j, b, text))
    old = cands[:-KEEP_RECENT] if len(cands) > KEEP_RECENT else []
    plan: list[tuple[int, int, ToolResultBlock, str, str, int]] = []
    total = 0
    for i, j, b, text in old:
        path = str(ctx.tool_results_dir / f"{b.tool_use_id}.txt")
        stub = stub_text(path)
        saved = estimate_text_tokens(text) - estimate_text_tokens(stub)
        if saved > 0:
            plan.append((i, j, b, text, stub, saved))
            total += saved
    if total < MIN_SAVINGS_TOKENS:
        return items, []

    # Persist first, then clear (lossless, 05#T4).
    ctx.tool_results_dir.mkdir(parents=True, exist_ok=True)
    out = list(items)
    reps: list[Replacement] = []
    for i, j, b, text, stub, saved in plan:
        p = ctx.tool_results_dir / f"{b.tool_use_id}.txt"
        if not p.exists():
            p.write_text(text, encoding="utf-8")
        msg = out[i]
        assert isinstance(msg, Message)
        blocks = list(msg.content)
        blocks[j] = replace(b, content=stub)
        out[i] = replace(msg, content=blocks)
        reps.append(Replacement(b.tool_use_id, stub, str(p), saved))
    if log:
        log_replacements(ctx, reps)
    return out, reps


def log_replacements(ctx: SessionContext, reps: list[Replacement]) -> None:
    """05 R34: one ``content-replacement`` row per replacement, replayed on resume."""
    store = ctx.transcript
    if store is not None and hasattr(store, "append_metadata"):
        for r in reps:
            store.append_metadata("content-replacement", r.to_row_value())


def replay_replacements(items: list[ConversationItem], replacements: list[dict[str, Any]]) -> list[ConversationItem]:
    """Re-apply logged replacements on resume (05 R34) so the request bytes match
    what the model saw before the restart (stable prompt-cache prefix)."""
    stubs = {r.get("toolUseId"): r.get("stub") for r in replacements if isinstance(r, dict)}
    if not stubs:
        return items
    out: list[ConversationItem] = []
    for it in items:
        if isinstance(it, Message) and any(isinstance(b, ToolResultBlock) and b.tool_use_id in stubs for b in it.content):
            blocks = [
                replace(b, content=stubs[b.tool_use_id]) if isinstance(b, ToolResultBlock) and b.tool_use_id in stubs else b
                for b in it.content
            ]
            out.append(replace(it, content=blocks))
        else:
            out.append(it)
    return out


def server_clearing_edit(idle_seconds: float, thinking_enabled: bool) -> dict[str, Any] | None:
    """``context_management`` payload (05 R31/R32) or None.

    * thinking on -> keep every thinking block (explicit policy, R31);
    * idle > 3900 s (the 1 h cache has expired, so the prefix is paid in full
      anyway) -> ask the server to clear old re-fetchable tool uses, keeping 5 and
      clearing at least 20k input tokens (R32, 05#T5).
    """
    edits: list[dict[str, Any]] = []
    if thinking_enabled:
        edits.append({"type": "clear_thinking", "keep": "all"})
    if idle_seconds > IDLE_CLEAR_AFTER_S:
        edits.append(
            {
                "type": "clear_tool_uses",
                "trigger": {"type": "tool_uses", "value": IDLE_CLEAR_TRIGGER_TOOL_USES},
                "keep": {"type": "tool_uses", "value": KEEP_RECENT},
                "clear_at_least": {"type": "input_tokens", "value": MIN_SAVINGS_TOKENS},
                "include_tools": sorted(CLEARABLE_TOOLS),
            }
        )
    return {"edits": edits} if edits else None
