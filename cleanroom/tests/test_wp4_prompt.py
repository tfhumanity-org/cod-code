"""WP4 - system prompt sections, memoization and cache split (02 R1-R9, R35-R36)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnesslab.context.prompt import (
    BOUNDARY,
    GLOBAL_CACHE,
    ORG_CACHE,
    PromptFacts,
    PromptOptions,
    Section,
    SectionCache,
    SystemPromptBuilder,
    default_sections,
    split_blocks,
    subagent_notes,
    validate_dynamic_body,
)


def _builder(**opts):
    return SystemPromptBuilder(PromptOptions(**opts))


def _text(blocks):
    return "\n\n".join(b.text for b in blocks)


def test_shared_section_order_and_boundary_split(ctx):
    blocks = _builder().build(ctx, [SimpleNamespace(name="TodoWrite")])
    # identity (uncached) + global shared block + org session block
    assert blocks[0].cache_control is None
    assert blocks[1].cache_control == GLOBAL_CACHE
    assert blocks[2].cache_control == ORG_CACHE
    shared = blocks[1].text
    order = [shared.index(h) for h in ("authorised security", "# System", "# Doing tasks", "# Acting carefully", "# Using tools", "# Tone")]
    assert order == sorted(order)
    assert "TodoWrite" in shared
    assert BOUNDARY not in _text(blocks)
    # volatile facts are not in the system prompt (02#T1)
    assert str(ctx.cwd) not in shared


def test_task_conduct_omitted_under_non_coding_style(ctx):
    ctx.extra["output_style"] = {"name": "explanatory", "body": "...", "keep_coding_instructions": False}
    text = _text(_builder().build(ctx, []))
    assert "# Doing tasks" not in text
    assert "'explanatory' output style" in text
    ctx.extra["output_style"] = {"name": "explanatory", "keep_coding_instructions": True}
    assert "# Doing tasks" in _text(SystemPromptBuilder().build(ctx, []))


def test_lean_trait_replaces_shared_sections(ctx):
    ctx.extra["model_traits"] = {"lean"}
    text = _text(_builder().build(ctx, []))
    assert "# Working here" in text and "# Tone" not in text


def test_memoization_by_variant_key():
    calls = []

    def compute(f):
        calls.append(f.output_style)
        return f"style {f.output_style}"

    s = Section("x", "x", "session", compute, qualifiers=lambda f: f.output_style or "")
    cache = SectionCache()
    f1 = PromptFacts("m", output_style="a")
    assert cache.get_or_compute(s, f1) == "style a"
    assert cache.get_or_compute(s, f1) == "style a"
    assert calls == ["a"]  # memoized
    assert cache.get_or_compute(s, PromptFacts("m", output_style="b")) == "style b"
    assert calls == ["a", "b"]  # new variant key -> recompute
    breaking = Section("y", "y", "session", compute, cache_break=True)
    cache.get_or_compute(breaking, f1)
    cache.get_or_compute(breaking, f1)
    assert len(calls) == 4
    cache.invalidate()
    cache.get_or_compute(s, f1)
    assert len(calls) == 5


def test_builder_memoizes_across_builds(ctx):
    b = _builder()
    b.build(ctx, [])
    n = b.cache.computations
    b.build(ctx, [])
    assert b.cache.computations == n


def test_split_blocks_missing_boundary_is_all_org():
    blocks = split_blocks(["a", "b"], header="billing: x", identity="I am")
    assert [b.cache_control for b in blocks] == [None, None, ORG_CACHE]
    assert blocks[2].text == "a\n\nb"
    blocks = split_blocks(["a", BOUNDARY, "b"], header=None, identity="I am")
    assert [b.cache_control for b in blocks] == [None, GLOBAL_CACHE, ORG_CACHE]


def test_bare_mode(ctx):
    ctx.env = {"CLAUDE_CODE_SIMPLE": "1"}
    ctx.extra["today"] = "2026-01-02"
    blocks = _builder().build(ctx, [])
    assert len(blocks) == 1 and blocks[0].text == f"CWD: {ctx.cwd}\nDate: 2026-01-02"


def test_resolution_order(ctx):
    t = _text(_builder(override_system_prompt="OVR", append_system_prompt="APP").build(ctx, []))
    assert t == "OVR"
    t = _text(_builder(custom_system_prompt="CUSTOM", append_system_prompt="APP").build(ctx, []))
    assert t.index("CUSTOM") < t.index("APP") and "# Tone" not in t
    t = _text(_builder(agent_prompt="AGENT", custom_system_prompt="CUSTOM").build(ctx, []))
    assert "AGENT" in t and "CUSTOM" not in t
    t = _text(_builder(agent_prompt="AGENT", agent_append_mode=True).build(ctx, []))
    assert "# Tone" in t and t.index("# Tone") < t.index("AGENT")
    t = _text(_builder(coordinator_mode=True, custom_system_prompt="CUSTOM").build(ctx, []))
    assert "coordinate" in t and "CUSTOM" not in t


def test_identity_variants(ctx):
    ctx.is_interactive = False
    t = _builder(append_system_prompt="extra").build(ctx, [])[0].text
    assert "SDK" in t
    assert ctx.extra["identity_prefix_kind"] == "sdk_append"


def test_subagent_notes(ctx):
    child = ctx.child(agent_id="a1", agent_type="general-purpose")
    text = _text(_builder().build(child, []))
    notes = subagent_notes()
    assert notes in text
    for needle in ("absolute", "emojis", "colon", "report", "consent"):
        assert needle in notes
    assert notes not in _text(_builder().build(ctx, []))


def test_host_resolvers(ctx):
    seen = []

    def per_section(sid, text):
        seen.append(sid)
        return text.upper() if sid == "tone" else text

    t = _text(_builder(section_resolver=per_section).build(ctx, []))
    assert "# TONE" in t and "intro" in seen
    t = _text(_builder(compose_resolver=lambda facts, parts: ["only " + facts.model_id]).build(ctx, []))
    assert t.endswith("only scripted-model")


def test_system_turn_variant(ctx):
    assert "<system-reminder>" in _text(_builder().build(ctx, []))
    ctx.extra["supports_system_turns"] = True
    assert "system turns" in _text(_builder().build(ctx, []))


def test_sdk_preset_static_memory(ctx):
    a = _builder(exclude_dynamic_sections=True).build(ctx, [])
    assert "# Memory" in a[1].text  # static memory moved into the shared part
    assert "memory directory at" not in _text(a)
    assert "memory directory at" in _text(_builder().build(ctx, []))
    validate_dynamic_body("# Heading\nbody")
    with pytest.raises(ValueError):
        validate_dynamic_body("no heading")


def test_default_sections_ids():
    ids = [s.id for s in default_sections() if s.scope == "shared"]
    assert ids[:6] == ["intro", "system", "task_conduct", "action_caution", "tool_usage", "tone"]


def test_output_style_loading(tmp_path):
    from harnesslab.context.prompt import load_output_styles

    home, cwd = tmp_path / "home", tmp_path / "proj"
    (home / ".claude" / "output-styles").mkdir(parents=True)
    (cwd / ".claude" / "output-styles").mkdir(parents=True)
    (home / ".claude" / "output-styles" / "pirate.md").write_text("---\nname: pirate\ndescription: arr\n---\nTalk like a pirate.")
    (cwd / ".claude" / "output-styles" / "p2.md").write_text("---\nname: pirate\nkeep-coding-instructions: true\n---\nProject pirate.")
    styles = load_output_styles(home, cwd)
    assert {"explanatory", "learning", "concise", "pirate"} <= set(styles)
    assert styles["pirate"].body == "Project pirate." and styles["pirate"].keep_coding_instructions
    assert styles["pirate"].as_extra()["name"] == "pirate"
