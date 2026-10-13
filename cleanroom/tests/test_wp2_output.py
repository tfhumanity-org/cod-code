"""WP2 output budgets and spill: 03 R8, R23-R26, §4.4 (03#T7), 05 R34."""

from __future__ import annotations

import os

from harnesslab.tools.base import Tool
from harnesslab.tools.builtin.bash import BashTool
from harnesslab.tools.builtin.read import ReadTool
from harnesslab.tools.output import (
    BATCH_BUDGET,
    PREVIEW_CHARS,
    empty_result_note,
    enforce_batch_budget,
    finalize_result,
    spill,
    spill_threshold,
)
from harnesslab.types import ImageBlock, Message, TextBlock, ToolResultBlock


class Small(Tool):
    name = "Small"
    max_result_chars = 1000


def test_thresholds(ctx):
    assert spill_threshold(Small(), ctx) == 1000
    assert spill_threshold(BashTool(), ctx) == 30_000
    assert spill_threshold(Tool(), ctx) == 50_000  # 100k default capped by the 50k ceiling
    assert spill_threshold(ReadTool(), ctx) is None  # Read self-limits
    ctx.extra["tool_result_limits"] = {"Small": 10}
    assert spill_threshold(Small(), ctx) == 10


def test_empty_result_note(ctx):
    r = finalize_result(Small(), ToolResultBlock("t1", "  "), ctx)
    assert r.content == empty_result_note("Small") == "(Small completed with no output)"


def test_spill_writes_file_and_previews_cut_at_newline(ctx):
    lines = [f"line {i:04d} " + "x" * 40 for i in range(200)]
    text = "\n".join(lines)
    r = finalize_result(Small(), ToolResultBlock("tu_big", text), ctx)
    body = r.content
    assert body.startswith("<persisted-output>") and body.endswith("</persisted-output>")
    path = ctx.tool_results_dir / "tu_big.txt"
    assert path.read_text() == text and str(path) in body
    preview = body.split("characters):\n", 1)[1].rsplit("\n...\n", 1)[0]
    assert len(preview) <= PREVIEW_CHARS and len(preview) >= PREVIEW_CHARS // 2
    assert text.startswith(preview) and text[len(preview)] == "\n"  # cut at a line boundary


def test_media_results_are_exempt(ctx):
    big = ToolResultBlock("t1", [ImageBlock("image/png", "A" * 5000), TextBlock("x" * 5000)])
    assert finalize_result(Small(), big, ctx) is big


def test_symlink_refusal_gives_truncated_output(ctx, tmp_path):
    ctx.tool_results_dir.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text("do not touch")
    os.symlink(target, ctx.tool_results_dir / "tu_evil.txt")
    out = spill("y" * 5000, "tu_evil", ctx)
    assert out.startswith("<truncated-output>") and "could not be saved" in out
    assert target.read_text() == "do not touch"


def test_respill_of_identical_content_reuses_file(ctx):
    a = spill("z" * 3000, "tu_same", ctx)
    b = spill("z" * 3000, "tu_same", ctx)
    assert a == b and a.startswith("<persisted-output>")


def msg(*blocks):
    return Message("user", list(blocks))


def test_batch_budget_spills_largest_fresh_first_and_memo_reapplies(ctx):
    big = ToolResultBlock("a", "A" * 120_000)
    mid = ToolResultBlock("b", "B" * 90_000)
    small = ToolResultBlock("c", "C" * 20_000)
    results = [msg(big), msg(mid), msg(small)]
    enforce_batch_budget(results, ctx)
    assert big.content.startswith("<persisted-output>")  # largest spilled first
    assert mid.content == "B" * 90_000 and small.content == "C" * 20_000  # now under budget
    assert sum(len(m.content[0].text()) for m in results) <= BATCH_BUDGET
    replacement = big.content

    # Later turn: the same results arrive again with original content (e.g. reloaded);
    # the memo re-applies byte-identically and kept results stay kept.
    again = [msg(ToolResultBlock("a", "A" * 120_000)), msg(ToolResultBlock("b", "B" * 90_000)), msg(ToolResultBlock("c", "C" * 20_000))]
    enforce_batch_budget(again, ctx)
    assert again[0].content[0].content == replacement
    assert again[1].content[0].content == "B" * 90_000


def test_batch_budget_skips_exempt_tools(ctx):
    class Exempt(Tool):
        name = "Web"
        skip_batch_budget = True

    huge = ToolResultBlock("w", "W" * 250_000)
    enforce_batch_budget([msg(huge)], ctx, {"w": Exempt()})
    assert huge.content == "W" * 250_000
