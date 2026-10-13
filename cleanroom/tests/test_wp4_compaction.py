"""WP4 - thresholds, controller breakers, summarizer, micro-compaction, re-injection
(05 R5-R35, §4.1-§4.6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnesslab.compaction import controller as C
from harnesslab.compaction import microcompact as MC
from harnesslab.compaction import reinject as R
from harnesslab.compaction import summarizer as S
from harnesslab.compaction import thresholds as T
from harnesslab.config import Settings
from harnesslab.hooks.types import AggregatedHookResult, HookEvent
from harnesslab.model.base import APIError
from harnesslab.model.scripted import ScriptedModel, say
from harnesslab.tokens import is_compact_boundary
from harnesslab.types import Attachment, ErrorKind, ImageBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage

# ------------------------------------------------------------------ helpers


def rounds(n: int, size: int = 400) -> list:
    """``n`` API rounds: user prompt, assistant (own message id)."""
    items: list = []
    for i in range(n):
        items.append(Message.user(f"question {i} " + "q" * size))
        items.append(Message.assistant(f"answer {i} " + "a" * size, message_id=f"msg{i}"))
    return items


def summarizer_fn(record=None):
    def fn(msgs, instructions):
        if record is not None:
            record.append((list(msgs), instructions))
        return "<analysis>private notes</analysis><summary>the summary</summary>"

    return fn


class FakeHooks:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def run(self, event, payload, ctx):
        self.calls.append((event, payload))
        return self.results.get(event, AggregatedHookResult())


# ------------------------------------------------------------------ thresholds


def test_worked_threshold_example():
    lv = T.levels(200_000, 200_000, 64_000, 0)
    assert (lv.effective, lv.threshold, lv.warn, lv.precompute, lv.blocked) == (180_000, 167_000, 147_000, 144_000, 177_000)
    assert lv.state == "ok"
    assert T.levels(200_000, 200_000, 64_000, 150_000).state == "warn"
    assert T.levels(200_000, 200_000, 64_000, 170_000).state == "compact"
    assert T.levels(200_000, 200_000, 64_000, 177_000).state == "blocked"
    assert T.levels(200_000, 200_000, 64_000, 0, pct_override=50).threshold == 90_000
    assert T.levels(200_000, 200_000, 64_000, 0, block_override=1234).blocked == 1234


def test_window_precedence():
    s = Settings.from_dict({"autoCompactWindow": {"m": 150_000, "default": 120_000}})
    assert T.resolve_window({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "5000"}, s, model_id="m", model_window=200_000).window == 100_000
    assert T.resolve_window({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "9000000"}, s, model_id="m", model_window=200_000).window == 1_000_000
    w = T.resolve_window({}, s, model_id="m", model_window=200_000)
    assert (w.window, w.source) == (150_000, "settings")
    assert T.resolve_window({}, s, model_id="x", model_window=200_000).window == 120_000
    w = T.resolve_window({}, Settings(), model_id="big", model_window=1_000_000)
    assert (w.window, w.source) == (200_000, "model-default")
    assert T.resolve_window({}, Settings(), model_id="big[1m]", model_window=1_000_000).window == 1_000_000
    assert T.resolve_window({}, Settings(), model_id="?", model_window=0).source == "unknown-model"


def test_disable_flags_and_status_text():
    assert T.compaction_disabled({"DISABLE_COMPACT": "1"})
    assert T.auto_compaction_disabled({"DISABLE_AUTO_COMPACT": "1"}, Settings())
    assert T.auto_compaction_disabled({}, Settings.from_dict({"autoCompactEnabled": False}))
    assert not T.auto_compaction_disabled({}, Settings())
    lv = T.levels(200_000, 200_000, 64_000, 83_500)
    assert T.status_text(lv, enforced=True) == "50% until auto-compact"
    assert T.status_text(lv, enforced=False).endswith("context used")
    assert "/compact" in T.status_text(T.levels(200_000, 200_000, 64_000, 150_000), enforced=True)


# ------------------------------------------------------------------ summarizer pieces


def test_split_into_api_rounds_keeps_split_rows_together():
    a1 = Message.assistant([ToolUseBlock("t1", "Read")], message_id="m1")
    a1b = Message.assistant("more", message_id="m1")
    r1 = Message.user([ToolResultBlock("t1", "x")])
    a2 = Message.assistant("done", message_id="m2")
    u0 = Message.user("hi")
    groups = S.split_into_api_rounds([u0, a1, a1b, r1, a2])
    assert groups == [[u0, a1, a1b, r1], [a2]]


def test_extract_summary_and_continuation():
    assert S.extract_summary_section("<analysis>x</analysis>\n<summary>\nS\n</summary>") == "S"
    m = S.continuation_message("S", "/t.jsonl", auto=True)
    assert m.is_compact_summary and "/t.jsonl" in m.text() and "Do not ask" in m.text()
    assert "Do not ask" not in S.continuation_message("S", "/t.jsonl", auto=False).text()


def test_summary_prompt_requirements():
    p = S.summary_prompt()
    for needle in ("<analysis>", "<summary>", "All user messages", "Pending tasks", "word for word", "attributed to the user"):
        assert needle in p
    assert "five recently read files" in S.summary_prompt("lean")
    assert "EARLIER" in S.summary_prompt(partial="up_to") and "RECENT" in S.summary_prompt(partial="from")


def test_ptl_retry_drops_20_percent_then_fails():
    groups = S.split_into_api_rounds(rounds(10))
    sizes = []

    def call(rs):
        sizes.append(len([g for g in rs if not S._is_marker(g)]))
        if len(sizes) < 3:
            raise APIError(ErrorKind.PROMPT_TOO_LONG)
        assert S._is_marker(rs[0])  # truncation marker in front
        return "ok"

    assert S.ptl_retry(groups, call) == "ok"
    assert sizes == [10, 8, 6]

    def always(rs):
        raise APIError(ErrorKind.PROMPT_TOO_LONG)

    with pytest.raises(S.CompactError):
        S.ptl_retry(groups, always)


def test_ptl_retry_uses_token_gap():
    groups = S.split_into_api_rounds(rounds(10, size=400))  # ~200 tokens per round
    seen = []

    def call(rs):
        seen.append(len([g for g in rs if not S._is_marker(g)]))
        if len(seen) == 1:
            raise APIError(ErrorKind.PROMPT_TOO_LONG, actual_tokens=1500, limit_tokens=1000)
        return "ok"

    S.ptl_retry(groups, call)
    # rounds are cut at assistant rows: [u0 a0 u1] ~309 tokens, then [a1 u2] ~206 ...
    assert seen == [10, 8]  # two rounds (~515 tokens) cover the 500-token gap


# ------------------------------------------------------------------ full compaction


def test_full_compaction_ordering(ctx):
    ctx.extra["active_goal"] = "ship it"
    readme = ctx.cwd / "README.md"
    readme.write_text("hello")
    ctx.ledger = SimpleNamespace(snapshot=lambda: [(str(readme), None)], clear=lambda: setattr(ctx, "_cleared", True))
    ctx.discovered_tools = {"WebFetch"}
    hooks = FakeHooks(
        {
            HookEvent.PRE_COMPACT: AggregatedHookResult(additional_contexts=["focus on tests"]),
            HookEvent.SESSION_START: AggregatedHookResult(additional_contexts=["project state"]),
        }
    )
    ctx.hook_runner = hooks
    items = rounds(3)
    record: list = []
    res = S.compact(items, ctx, trigger="auto", summarize_fn=summarizer_fn(record), custom_instructions="keep API notes")
    kinds = [type(x).__name__ if not isinstance(x, Attachment) else x.type for x in res.items]
    assert is_compact_boundary(res.items[0]) and res.items[0].parent_uuid is None
    assert res.items[1].is_compact_summary and "the summary" in res.items[1].text() and "private notes" not in res.items[1].text()
    assert kinds[2:] == ["compact_file_reference", "goal", "hook_additional_context"]
    meta = res.items[0].extra["compactMetadata"]
    assert meta["trigger"] == "auto" and meta["preTokens"] > 0 and meta["preCompactDiscoveredTools"] == ["WebFetch"]
    assert res.items[0].extra["logicalParentUuid"] == items[-1].uuid
    _, instructions = record[0]
    assert "keep API notes" in instructions and "focus on tests" in instructions
    assert [e for e, _ in hooks.calls] == [HookEvent.PRE_COMPACT, HookEvent.SESSION_START, HookEvent.POST_COMPACT]
    assert getattr(ctx, "_cleared", False)


def test_precompact_block(ctx):
    ctx.hook_runner = FakeHooks({HookEvent.PRE_COMPACT: AggregatedHookResult(blocking_errors=["no"])})
    with pytest.raises(S.CompactBlocked):
        S.compact(rounds(2), ctx, trigger="manual", summarize_fn=summarizer_fn())


def test_model_summarizer_cold_path(ctx, scripted):
    scripted.add(say("<summary>from the model</summary>"))
    items = rounds(2) + [Message.user([ImageBlock("image/png", "AAAA")])]
    res = S.compact(items, ctx, trigger="manual")
    assert "from the model" in res.items[1].text()
    req = scripted.requests[0]
    assert req.query_source == "compact" and req.tools == []
    assert "image" not in str(req.messages)  # media stripped on the cold path


def test_model_summarizer_warm_fork(ctx, scripted):
    from harnesslab.model.base import SystemBlock

    ctx.extra["cache_safe_params"] = {"system": [SystemBlock("MAIN PROMPT")], "tools": [{"name": "Read"}]}
    scripted.add(say("<summary>warm</summary>"))
    S.compact(rounds(2), ctx, trigger="auto")
    req = scripted.requests[0]
    assert req.system[0].text == "MAIN PROMPT" and req.tools == [{"name": "Read"}]
    assert req.metadata.get("deny_all_tools")


# ------------------------------------------------------------------ reactive / partial


def test_reactive_keeps_recent_rounds_verbatim(ctx):
    items = rounds(6, size=400)  # each round ~200 tokens
    record: list = []
    res = S.reactive_compact(items, ctx, token_gap=500, summarize_fn=summarizer_fn(record))
    summarized = record[0][0]
    # groups: [u0 a0 u1] [a1 u2] [a2 u3] [a3 u4] [a4 u5] [a5]; the newest two fit under the gap
    assert len(summarized) == 9
    texts = [x.text() for x in res.items if isinstance(x, Message)]
    assert "verbatim" in texts[2] or "word for word" in texts[2]
    tail = res.items[3:]
    assert [m.text()[:10] for m in tail] == ["answer 4 a", "question 5", "answer 5 a"]
    assert all(m.uuid not in {i.uuid for i in items} for m in tail)  # fresh rows
    assert "Do not ask" in res.items[1].text()


def test_reactive_last_resort_and_impossible(ctx):
    def ptl_unless_small(msgs, instr):
        if len(msgs) > 3:
            raise APIError(ErrorKind.PROMPT_TOO_LONG)
        return "<summary>first round only</summary>"

    res = S.reactive_compact(rounds(4), ctx, token_gap=None, summarize_fn=ptl_unless_small)
    assert "first round only" in res.items[1].text()
    assert S.reactive_compact(rounds(1), ctx, token_gap=None, summarize_fn=summarizer_fn()) is None


def test_partial_compaction(ctx):
    items = rounds(4)
    pivot = items[4].uuid
    res = S.partial_compact(items, ctx, pivot_uuid=pivot, direction="up_to", note="mind the API", summarize_fn=summarizer_fn())
    seg = res.items[0].extra["compactMetadata"]["preservedSegment"]
    assert seg["anchorUuid"] == res.items[1].uuid and seg["headUuid"] == res.items[2].uuid and seg["tailUuid"] == res.items[-1].uuid
    assert len(res.items) == 2 + 4
    res = S.partial_compact(items, ctx, pivot_uuid=pivot, direction="from", summarize_fn=summarizer_fn())
    assert res.items[-1].is_compact_summary and len(res.items) == 1 + 4 + 1


# ------------------------------------------------------------------ controller


def big_items(tokens: int) -> list:
    return [Message.user("x"), Message.assistant("y", usage=Usage(input_tokens=tokens), message_id="big")]


def test_controller_compacts_over_threshold(ctx):
    comp = C.AutoCompactor(summarize_fn=summarizer_fn(), max_output=64_000)
    assert comp.maybe_autocompact(big_items(100_000), ctx) is None
    res = comp.maybe_autocompact(big_items(170_000), ctx)
    assert res is not None and res.trigger == "auto"
    ctx.env = {"DISABLE_AUTO_COMPACT": "1"}
    assert comp.maybe_autocompact(big_items(170_000), ctx) is None


def test_failure_breaker(ctx):
    def boom(msgs, instr):
        raise APIError(ErrorKind.SERVER_ERROR)

    comp = C.AutoCompactor(summarize_fn=boom, max_output=64_000)
    for _ in range(3):
        assert comp.maybe_autocompact(big_items(170_000), ctx) is None
    assert comp.state(ctx).consecutive_failures == 3
    comp.summarize_fn = summarizer_fn()
    assert comp.maybe_autocompact(big_items(170_000), ctx) is None  # breaker open for the session


def test_thrash_breaker(ctx):
    comp = C.AutoCompactor(summarize_fn=summarizer_fn(), max_output=64_000)
    results = [comp.maybe_autocompact(big_items(170_000), ctx) for _ in range(5)]
    # 1st compacts; each refill within <3 turns is a strike; the 3rd strike trips the breaker
    assert [r is not None for r in results] == [True, True, True, False, False]
    assert comp.thrashing(ctx)
    assert "too large" in comp.state(ctx).notices[0]


def test_slow_refill_is_not_thrash(ctx):
    comp = C.AutoCompactor(summarize_fn=summarizer_fn(), max_output=64_000)
    for _ in range(4):
        assert comp.maybe_autocompact(big_items(170_000), ctx) is not None
        for _ in range(3):
            comp.maybe_autocompact(big_items(1_000), ctx)
    assert not comp.thrashing(ctx)


def test_prefix_too_large(ctx):
    ctx.extra["prefix_tokens"] = 180_000
    comp = C.AutoCompactor(summarize_fn=summarizer_fn(), max_output=64_000)
    assert comp.maybe_autocompact(big_items(170_000), ctx) is None
    assert comp.state(ctx).prefix_too_large and "cannot help" in comp.state(ctx).notices[0]


def test_classifier_overflow_forces_compaction(ctx):
    ctx.extra["classifier_context_overflowed"] = True
    comp = C.AutoCompactor(summarize_fn=summarizer_fn())
    res = comp.maybe_autocompact(big_items(1_000), ctx)
    assert res is not None and res.items[-1].type == "critical_system_reminder"
    assert "again" in res.items[-1].payload["content"]


def test_precomputed_swap(ctx):
    comp = C.AutoCompactor(summarize_fn=summarizer_fn(), max_output=64_000)
    items = big_items(150_000)
    pre = comp.precompute(items, ctx)
    assert pre is not None
    later = items + [Message.user("new question")]
    res = comp.maybe_autocompact(later + [Message.assistant("z", usage=Usage(input_tokens=170_000), message_id="m9")], ctx)
    assert res.trigger == "precomputed"
    assert any(isinstance(m, Message) and m.text() == "new question" for m in res.items)
    # stale precompute (head changed) is discarded
    comp.precompute(items, ctx)
    other = big_items(170_000)
    assert comp.maybe_autocompact(other, ctx).trigger == "auto"


def test_reactive_via_controller(ctx):
    comp = C.AutoCompactor(summarize_fn=summarizer_fn())
    assert comp.reactive(rounds(4), ctx, None).trigger == "reactive"
    ctx.env = {"DISABLE_COMPACT": "1"}
    assert comp.reactive(rounds(4), ctx, None) is None


# ------------------------------------------------------------------ micro-compaction


def tool_rounds(n: int, chars: int) -> list:
    items: list = []
    for i in range(n):
        items.append(Message.assistant([ToolUseBlock(f"t{i}", "Read", {"file_path": f"/f{i}"})], message_id=f"m{i}"))
        items.append(Message.user([ToolResultBlock(f"t{i}", "r" * chars)]))
    return items


def test_microcompact_keep5_threshold_and_spill(ctx):
    small = tool_rounds(8, 1000)
    assert MC.microcompact(small, ctx) == (small, [])  # saves < 20k tokens
    items = tool_rounds(8, 40_000)  # 10k tokens each, 3 clearable -> ~30k saved
    out, reps = MC.microcompact(items, ctx)
    assert [r.tool_use_id for r in reps] == ["t0", "t1", "t2"]
    first = out[1].content[0]
    assert "Read tool" in first.content and reps[0].path in first.content
    with open(reps[0].path) as fh:
        assert fh.read() == "r" * 40_000
    assert out[-1].content[0].content == "r" * 40_000  # recent 5 untouched
    assert items[1].content[0].content == "r" * 40_000  # input not mutated
    ctx.env = {"DISABLE_MICROCOMPACT": "1"}
    assert MC.microcompact(items, ctx) == (items, [])


def test_microcompact_logs_and_replays(ctx):
    rows = []
    ctx.transcript = SimpleNamespace(append_metadata=lambda t, v: rows.append((t, v)))
    items = tool_rounds(8, 40_000)
    out, reps = MC.microcompact(items, ctx)
    assert [t for t, _ in rows] == ["content-replacement"] * 3
    replayed = MC.replay_replacements(items, [v for _, v in rows])
    assert [m.content for m in replayed] == [m.content for m in out]


def test_server_clearing_edit():
    assert MC.server_clearing_edit(10, False) is None
    e = MC.server_clearing_edit(4000, True)["edits"]
    assert e[0]["keep"] == "all" and e[1]["keep"]["value"] == 5 and e[1]["clear_at_least"]["value"] == 20_000


# ------------------------------------------------------------------ re-injection


def test_reinjection_budgets(ctx):
    files = []
    for i in range(7):
        p = ctx.cwd / f"f{i}.txt"
        p.write_text(("line\n" * 6000) if i == 6 else f"file {i}")
        files.append((str(p), None))
    kept = [Message.assistant([ToolUseBlock("k", "Read", {"file_path": files[5][0]})])]
    atts = R.build_reinjection(ctx, files, kept)
    paths = [a.payload["path"] for a in atts if a.type == "compact_file_reference"]
    # newest first, f5 excluded (already in kept), max 5 files
    assert [p.rsplit("/", 1)[1] for p in paths] == ["f6.txt", "f4.txt", "f3.txt", "f2.txt", "f1.txt"]
    big = next(a for a in atts if a.payload["path"].endswith("f6.txt"))
    assert big.payload["truncated"] and len(big.payload["content"]) <= R.FILE_TOKENS * 4 + 200

    ctx.invoked_skills = [{"name": f"s{i}", "body": "b" * 30_000, "path": f"/skills/s{i}.md"} for i in range(6)]
    skills = R.skill_attachment(ctx)[0].payload["skills"]
    assert [s["name"] for s in skills] == ["s5", "s4", "s3", "s2", "s1"]  # 5k each, 25k total
    assert "/skills/s5.md" in skills[0]["body"]


def test_reinjection_plan_tasks_mcp_deferred(ctx):
    from harnesslab.agents.tasks import TaskRegistry
    from harnesslab.permissions.types import PermissionMode

    plan = ctx.cwd / "plan.md"
    plan.write_text("the plan")
    ctx.plan_file = plan
    ctx.permissions.mode = PermissionMode.PLAN
    ctx.tasks = TaskRegistry()
    ctx.tasks.register("shell", "npm test")
    ctx.mcp_clients = [SimpleNamespace(name="db", instructions="use read replicas")]
    ctx.tools = [SimpleNamespace(name="WebFetch", should_defer=True)]
    types = [a.type for a in R.build_reinjection(ctx, [(str(plan), None)], [])]
    assert types == ["plan_file_reference", "plan_mode", "task_status", "mcp_instructions", "deferred_tools_delta"]
