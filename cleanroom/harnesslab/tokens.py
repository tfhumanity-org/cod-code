"""Token estimation and context-size anchoring (FINAL - architect).

Implemented now because the loop (WP1), compaction (WP4), tool output budgets
(WP2) and MCP output caps (WP5) all need the same numbers.

Spec coverage
-------------
* 01 R47 / 05 R1  context size = last real API usage total (input + cache
  creation + cache read + output) + a character estimate for every row after
  it. Rows split from the same response collapse to the earliest index. A
  compact boundary resets the anchor to zero.
* 01 R48 / 05 R2  ~4 chars/token for text, 2 for JSON, a flat 2000 tokens per
  image or document, tool_use = name + serialized input.
* 05 R4  large-turn flag when the last usage exceeds 200 000 tokens.
"""

from __future__ import annotations

import json

from harnesslab.types import (
    Attachment,
    ConversationItem,
    DocumentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolReferenceBlock,
    ToolResultBlock,
    ToolUseBlock,
    render_attachment_text,
)

CHARS_PER_TOKEN = 4
CHARS_PER_TOKEN_JSON = 2
MEDIA_TOKENS = 2000
LARGE_TURN_TOKENS = 200_000


def estimate_text_tokens(text: str, *, is_json: bool = False) -> int:
    per = CHARS_PER_TOKEN_JSON if is_json else CHARS_PER_TOKEN
    return round(len(text) / per)


def _block_tokens(b: object) -> int:
    if isinstance(b, TextBlock):
        return estimate_text_tokens(b.text)
    if isinstance(b, ThinkingBlock):
        return estimate_text_tokens(b.thinking)
    if isinstance(b, ToolUseBlock):
        return estimate_text_tokens(b.name) + estimate_text_tokens(json.dumps(b.input), is_json=True)
    if isinstance(b, ToolResultBlock):
        if isinstance(b.content, str):
            return estimate_text_tokens(b.content)
        return sum(_block_tokens(x) for x in b.content)
    if isinstance(b, (ImageBlock, DocumentBlock)):
        return MEDIA_TOKENS
    if isinstance(b, ToolReferenceBlock):
        return estimate_text_tokens(b.tool_name)
    return 0


def estimate_item_tokens(item: ConversationItem) -> int:
    if isinstance(item, Attachment):
        return estimate_text_tokens(render_attachment_text(item) or "")
    return sum(_block_tokens(b) for b in item.content)


def is_compact_boundary(item: ConversationItem) -> bool:
    return isinstance(item, Message) and item.role == "system" and item.subtype == "compact_boundary"


def context_tokens(items: list[ConversationItem]) -> int:
    """Anchored estimate of the current context size (01 R47)."""
    start = 0
    for i in range(len(items) - 1, -1, -1):
        if is_compact_boundary(items[i]):
            start = i + 1
            break
    anchor_idx, anchor_total, anchor_mid = None, 0, None
    for i in range(len(items) - 1, start - 1, -1):
        it = items[i]
        if (
            isinstance(it, Message)
            and it.role == "assistant"
            and it.usage is not None
            and not it.is_api_error
            and it.usage.context_total() > 0
        ):
            anchor_idx, anchor_total, anchor_mid = i, it.usage.context_total(), it.message_id
            break
    if anchor_idx is None:
        return sum(estimate_item_tokens(x) for x in items[start:])
    tail = [
        x
        for x in items[anchor_idx + 1 :]
        # rows split from the anchoring response are already in its usage
        if not (anchor_mid and isinstance(x, Message) and x.message_id == anchor_mid)
    ]
    return anchor_total + sum(estimate_item_tokens(x) for x in tail)


def is_large_turn(items: list[ConversationItem]) -> bool:
    for it in reversed(items):
        if isinstance(it, Message) and it.role == "assistant" and it.usage is not None:
            return it.usage.context_total() > LARGE_TURN_TOKENS
    return False
