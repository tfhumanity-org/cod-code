"""Output size budgets and spill-to-disk (WP2).

Implements spec 03 R8 (empty-result note), R23-R26 and 03 §4.4; 05 R34 (each
replacement is memoised so resumed requests stay byte-identical); takeaway
03#T7; P13 "spill, don't truncate".

* ``finalize_result``: threshold ``min(tool.max_result_chars, tool.persistence_ceiling
  or 50_000)``; media blocks exempt; over threshold -> ``spill``.
* ``spill``: write to ``ctx.tool_results_dir / f"{tool_use_id}.{txt|json}"`` with
  exclusive create refusing symlinks/hardlinks (``os.O_EXCL | O_NOFOLLOW``);
  return a ``<persisted-output>`` block with size, path and a ~2 KB preview cut back
  to a newline if that keeps >= 50 %; on OSError return ``<truncated-output>``.
* ``enforce_batch_budget``: all results of one user message > 200 000 chars ->
  spill the largest *fresh* results first; replacements memoised by tool_use_id in
  ``ctx.spill_memo`` and re-applied identically on later turns (03 R25).

Design choices (spec silent)
----------------------------
* Tools whose ``max_result_chars`` is ``UNBOUNDED`` (Read) never spill: they
  self-limit by tokens, and spilling a Read result into a file the model would
  then Read again would loop.
* Per-tool remote overrides (R23 "remotely overridable") come from
  ``ctx.extra["tool_result_limits"]`` = ``{tool_name: chars}``.
* ``ctx.spill_memo`` records *every* decision of a batch: a replacement string
  for spilled results and ``KEEP`` for results judged and kept. A result that
  was kept once is never spilled later, so earlier request prefixes never
  change (05 R34).
* Re-spilling an id whose file already exists with identical content (e.g. a
  resumed session that lost its memo) reuses the file instead of failing.
"""

from __future__ import annotations

import os
from pathlib import Path

from harnesslab.runtime import SessionContext
from harnesslab.tools.base import UNBOUNDED, Tool
from harnesslab.types import DocumentBlock, ImageBlock, Message, TextBlock, ToolResultBlock
from harnesslab.util.text import human_size

DEFAULT_CEILING = 50_000
BASH_CEILING = 128_000
BATCH_BUDGET = 200_000
PREVIEW_CHARS = 2000
SHELL_OUTPUT_DISK_CAP = 64 * 1024 * 1024

PERSISTED_OPEN = "<persisted-output>"
TRUNCATED_OPEN = "<truncated-output>"
#: memo marker: "this result was judged and kept as is".
KEEP = False


def empty_result_note(tool_name: str) -> str:
    """R8: the model never sees an empty tool result."""
    return f"({tool_name} completed with no output)"


def is_empty_result(block: ToolResultBlock) -> bool:
    if isinstance(block.content, str):
        return not block.content.strip()
    return all(isinstance(b, TextBlock) and not b.text.strip() for b in block.content)


def has_media(block: ToolResultBlock) -> bool:
    return isinstance(block.content, list) and any(isinstance(b, (ImageBlock, DocumentBlock)) for b in block.content)


def result_chars(block: ToolResultBlock) -> int:
    """Size used by the budgets: characters of text (media counted as 0)."""
    return len(block.text())


def spill_threshold(tool: Tool, ctx: SessionContext) -> int | None:
    """R23 threshold, or ``None`` when the tool never spills."""
    if tool.max_result_chars >= UNBOUNDED:
        return None
    limit = min(tool.max_result_chars, tool.persistence_ceiling or DEFAULT_CEILING)
    override = (ctx.extra.get("tool_result_limits") or {}).get(tool.name)
    if isinstance(override, int) and override > 0:
        limit = override
    return limit


def finalize_result(tool: Tool, block: ToolResultBlock, ctx: SessionContext) -> ToolResultBlock:
    """Empty-result substitution (R8) then size budget / spill (R23-R24)."""
    if is_empty_result(block):
        return ToolResultBlock(block.tool_use_id, empty_result_note(tool.name), block.is_error)
    if has_media(block):
        return block  # images / documents are exempt (R23)
    limit = spill_threshold(tool, ctx)
    text = block.text()
    if limit is None or len(text) <= limit:
        return block
    if not isinstance(block.content, str) and not all(isinstance(b, TextBlock) for b in block.content):
        return block  # e.g. tool_reference lists: nothing sensible to spill
    return ToolResultBlock(block.tool_use_id, spill(text, block.tool_use_id, ctx), block.is_error)


def _preview(text: str) -> str:
    head = text[:PREVIEW_CHARS]
    nl = head.rfind("\n")
    if len(text) > PREVIEW_CHARS and nl >= PREVIEW_CHARS // 2:
        head = head[:nl]  # cut back to a line boundary if that keeps >= 50 %
    return head


def _exclusive_write(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively; refuse to follow or reuse links (R24)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd != -1:
            os.close(fd)


def _same_regular_file(path: Path, data: bytes) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size != len(data):
        return False
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def spill(text: str, tool_use_id: str, ctx: SessionContext, *, ext: str = "txt") -> str:
    """Return the replacement text (persisted-output or truncated-output block)."""
    data = text.encode("utf-8", "surrogatepass")[:SHELL_OUTPUT_DISK_CAP]
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool_use_id) or "result"
    path = ctx.tool_results_dir / f"{safe_id}.{ext}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _exclusive_write(path, data)
        except FileExistsError:
            if not _same_regular_file(path, data):
                raise
    except OSError:
        return (
            f"{TRUNCATED_OPEN}\n"
            f"The full output ({human_size(len(data))}) was too large for the context and could not "
            f"be saved to disk, so only the first {PREVIEW_CHARS} characters are shown. Re-run with "
            "filtering or pagination (e.g. grep, head, offset/limit) to see the part you need.\n"
            f"{text[:PREVIEW_CHARS]}\n"
            f"</truncated-output>"
        )
    head = _preview(text)
    return (
        f"{PERSISTED_OPEN}\n"
        f"Output too large for the context ({human_size(len(data))}). The full output was saved to: {path}\n"
        f"Use Read (with offset/limit) or Grep on that file to see the rest.\n\n"
        f"Preview (first {len(head)} characters):\n"
        f"{head}\n"
        f"...\n"
        f"</persisted-output>"
    )


def enforce_batch_budget(
    results: list[Message], ctx: SessionContext, tools_by_id: dict[str, Tool] | None = None
) -> list[Message]:
    """03 §4.4 ``enforce_batch_budget``: per-message aggregate budget (R25).

    Mutates the result blocks in place and returns ``results`` for chaining.
    """
    tools_by_id = tools_by_id or {}
    memo = ctx.spill_memo
    blocks = [b for m in results for b in m.content if isinstance(b, ToolResultBlock)]

    # 1. Re-apply earlier decisions byte-identically.
    for b in blocks:
        prior = memo.get(b.tool_use_id)
        if isinstance(prior, str):
            b.content = prior

    def exempt(b: ToolResultBlock) -> bool:
        t = tools_by_id.get(b.tool_use_id)
        return bool(t is not None and t.skip_batch_budget) or has_media(b)

    counted = [b for b in blocks if not exempt(b)]
    total = sum(result_chars(b) for b in counted)
    fresh = [b for b in counted if b.tool_use_id not in memo]

    # 2. Spill the largest fresh results until under budget.
    for b in sorted(fresh, key=result_chars, reverse=True):
        if total <= BATCH_BUDGET:
            break
        text = b.text()
        if text.startswith((PERSISTED_OPEN, TRUNCATED_OPEN)):
            continue
        before = len(text)
        b.content = spill(text, b.tool_use_id, ctx)
        memo[b.tool_use_id] = b.content
        total -= before - result_chars(b)

    # 3. Everything else judged in this batch is frozen as "keep".
    for b in fresh:
        memo.setdefault(b.tool_use_id, KEEP)
    return results
