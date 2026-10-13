"""WP5 - skills discovery/listing/resolution, the Skill tool, MCP hygiene and caps (spec 07 R1-R27)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from harnesslab.agents.notifications import NotificationQueue
from harnesslab.agents.tasks import TaskRegistry
from harnesslab.extensions.mcp import (
    UNKNOWN_OUTCOME_TEXT,
    InProcessMcpServer,
    McpServerConfig,
    McpToolSpec,
    api_accepts,
    cap_output,
    connection_plan,
    flatten_combinators,
    mcp_tool_name,
    normalize_server_name,
    register_tools,
    strip_invoke_markup,
    tool_timeouts,
    validate_managed_config,
)
from harnesslab.extensions.skill_tool import SkillTool
from harnesslab.extensions.skills import (
    Command,
    SkillResolutionError,
    discover_near,
    discover_skills,
    edit_distance,
    listing_budget,
    mention_attachment,
    parse_skill_file,
    path_scoped_active,
    render_body,
    resolve_skill,
    skill_listing,
)
from harnesslab.tools.base import ToolCallContext
from harnesslab.types import ImageBlock, TextBlock, render_attachment_text


def skill(root: Path, rel: str, front: str = "", body: str = "Do the thing.") -> Path:
    p = root / rel / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}\n---\n{body}\n" if front else body)
    return p


def tcx(ctx):
    return ToolCallContext(ctx, "toolu_s", ctx.abort.signal)


# ------------------------------------------------------------------ discovery (R1-R3)


def test_discovery_namespacing_plugins_and_realpath_dedupe(tmp_path):
    user = tmp_path / "user"
    skill(user, "deploy", "description: Deploy the app\nwhen_to_use: when shipping")
    skill(user, "db/migrate", "description: Run migrations")
    (user / "huge").mkdir()
    (user / "huge" / "SKILL.md").write_text("x" * 1_000_001)
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(user / "deploy", proj / "deploy-link")  # same file through two roots
    legacy = tmp_path / "commands"
    (legacy / "both").mkdir(parents=True)
    (legacy / "both" / "SKILL.md").write_text("---\ndescription: wins\n---\nA")
    (legacy / "both" / "other.md").write_text("---\ndescription: loses\n---\nB")
    (legacy / "old.md").write_text("---\ndescription: legacy\n---\nC")
    plug = tmp_path / "plug"
    skill(plug / "skills", "lint", "description: Lint code")
    found = discover_skills([(user, "user"), (proj, "project"), (legacy, "user")], plugin_roots={"acme": plug})
    by = {c.name: c for c in found}
    assert set(by) == {"deploy", "db:migrate", "both", "old", "acme:lint"}
    assert by["deploy"].when_to_use == "when shipping" and by["deploy"].source == "user"
    assert by["old"].loaded_from == "commands_DEPRECATED" and by["both"].description == "wins"
    assert by["acme:lint"].source == "plugin"


def test_plugin_skill_with_toplevel_security_hook_gets_nothing(tmp_path):
    p = skill(tmp_path / "skills", "evil", "description: x\nallowed-tools: Bash\nPreToolUse: something")
    c = parse_skill_file(p, base=tmp_path / "skills", source="plugin", plugin="bad")
    assert c.allowed_tools == [] and c.hooks == {}


def test_dynamic_discovery_and_paths(tmp_path):
    sub = tmp_path / "pkg" / "inner"
    skill(tmp_path / "pkg" / ".claude" / "skills", "local", "description: Local helper\npaths: ['*.py']")
    (sub).mkdir(parents=True)
    found = discover_near([str(sub / "file.py")], [], tmp_path)
    assert [c.name for c in found] == ["local"]
    assert discover_near([str(sub / "file.py")], found, tmp_path) == []
    assert path_scoped_active(found[0], [str(sub / "a.py")], tmp_path)
    assert not path_scoped_active(found[0], [str(sub / "a.md")], tmp_path)


# ------------------------------------------------------------------ listing (R6, §4.1)


def mk(name, desc="d" * 50, **kw):
    return Command(name=name, description=desc, **kw)


def test_listing_budget_formula():
    assert listing_budget(200_000) == 8000
    assert listing_budget(200_000, env_budget=500) == 500


def test_listing_fits_and_description_cap_and_when_to_use():
    s = [mk("a", "short", when_to_use="use it"), mk("b", "x" * 3000), mk("hidden", disable_model_invocation=True)]
    out = skill_listing(s, budget=100_000)
    assert "- a: short - use it" in out and "hidden" not in out
    line_b = next(line for line in out.splitlines() if line.startswith("- b:"))
    assert len(line_b) <= len("- b: ") + 1536


def test_listing_degrades_to_name_only_bundled_keep_descriptions():
    skills = [mk(f"s{i}", "y" * 100, last_used=float(i)) for i in range(10)] + [mk("core", "z" * 100, bundled=True)]
    out = skill_listing(skills, budget=600)
    lines = out.splitlines()[1:]
    assert any(line.startswith("- core: ") for line in lines)  # pinned
    names_only = [line for line in lines if ":" not in line]
    full = [line for line in lines if line.startswith("- s") and ":" in line]
    assert len(lines) == 11 and names_only and full  # nothing dropped
    assert full[0].startswith("- s9")  # most recently used kept its description


# ------------------------------------------------------------------ resolution (R7, §4.2)


def test_resolution_rules():
    skills = [mk("deploy"), mk("a:lint"), mk("b:lint"), mk("db:migrate", aliases=["mig"]), mk("secret", disable_model_invocation=True)]
    assert resolve_skill("/deploy", skills).name == "deploy"
    assert resolve_skill("mig", skills).name == "db:migrate"
    assert resolve_skill("migrate", skills).name == "db:migrate"
    with pytest.raises(SkillResolutionError) as e:
        resolve_skill("lint", skills)
    assert e.value.kind == "ambiguous" and e.value.candidates == ["a:lint", "b:lint"]
    with pytest.raises(SkillResolutionError) as e:
        resolve_skill("deplyo", skills)
    assert e.value.kind == "unknown" and "deploy" in e.value.candidates
    with pytest.raises(SkillResolutionError) as e:
        resolve_skill("acme:thing", skills, failed_plugins={"acme"})
    assert e.value.kind == "plugin_failed" and "failed to load" in str(e.value)
    with pytest.raises(SkillResolutionError) as e:
        resolve_skill("secret", skills)
    assert e.value.kind == "user_only"
    with pytest.raises(SkillResolutionError) as e:
        resolve_skill("deploy", skills, running_skill="deploy")
    assert e.value.kind == "already_running" and "follow" in str(e.value)
    assert edit_distance("kitten", "sitting") == 3


def test_render_body_substitutions(tmp_path):
    c = Command("x", "d", body="Run $ARGUMENTS in ${CLAUDE_SKILL_DIR} with ${CLAUDE_PLUGIN_ROOT}", base_dir=tmp_path)
    assert render_body(c, "tests", plugin_root=Path("/plug")) == f"Run tests in {tmp_path} with /plug"
    c2 = Command("y", "d", body="No placeholder")
    assert render_body(c2, "abc").endswith("ARGUMENTS: abc")
    c3 = Command("z", "d", body="B", bundled=True, has_files=True, base_dir=tmp_path)
    assert render_body(c3).startswith(f"Base directory for this skill: {tmp_path}")


def test_mention_attachment():
    att = mention_attachment("please run /deploy now, not /unknown", [mk("deploy")])
    assert att.payload["names"] == ["deploy"]
    assert "only if the user is asking" in render_attachment_text(att)
    assert mention_attachment("no slash here", [mk("deploy")]) is None


# ------------------------------------------------------------------ Skill tool (R8-R10, R12)


def test_skill_tool_inline_layers(ctx):
    ctx.skills = [Command("fmt", "Format", body="Format $ARGUMENTS", allowed_tools=["Bash(npm run fmt)"], model="haiku", effort="low")]
    out = SkillTool().call({"skill": "fmt", "args": "src/"}, tcx(ctx))
    assert not out.is_error and out.data["status"] == "inline"
    assert out.new_messages[0].is_meta and "Format src/" in out.new_messages[0].text()
    kinds = {layer.kind: layer for layer in out.context_layers}
    assert kinds["allowed_tools"].value == ["Bash(npm run fmt)"] and kinds["model"].value == "haiku" and kinds["effort"].value == "low"
    assert all(layer.until == "next_user_message" for layer in out.context_layers)
    assert ctx.invoked_skills[-1]["name"] == "fmt"
    assert "built-in" in SkillTool().prompt().lower()


def test_skill_tool_errors_are_tool_errors(ctx):
    ctx.skills = [mk("deploy")]
    out = SkillTool().call({"skill": "deplyo"}, tcx(ctx))
    assert out.is_error and "deploy" in out.data


def test_skill_tool_coordinator_read_only(ctx):
    ctx.extra["coordinator"] = True
    ctx.skills = [Command("f", "d", body="B", allowed_tools=["Bash"], model="opus", context="fork")]
    out = SkillTool().call({"skill": "f"}, tcx(ctx))
    assert out.data["readOnly"] and out.context_layers == [] and out.data["status"] == "inline"


class FakeRunner:
    def __init__(self, status="completed"):
        self.calls = []
        self.status = status

    def run_subagent(self, ctx, *, prompt, agent_type="general-purpose", background=False, description=""):
        self.calls.append((prompt, agent_type, background, ctx.extra.get("pending_skill_fork")))
        if self.status == "async_launched":
            return {"status": "async_launched", "agentId": "a1"}
        return {"status": "completed", "agentId": "a2", "content": "forked result"}


def test_skill_tool_fork_and_background(ctx):
    ctx.skills = [Command("research", "d", body="Dig into $ARGUMENTS", context="fork", agent="Explore")]
    r = FakeRunner()
    out = SkillTool(r).call({"skill": "research", "args": "auth"}, tcx(ctx))
    assert out.data == {"status": "forked", "agentId": "a2", "result": "forked result"}
    assert r.calls[0] == ("Dig into auth", "Explore", False, "research")
    assert "pending_skill_fork" not in ctx.extra
    ctx.skills[0].background = True
    out = SkillTool(FakeRunner("async_launched")).call({"skill": "research"}, tcx(ctx))
    assert out.data["background"] and "notification" in out.blocks[0].text


def test_skill_fork_recursion_guard_via_launcher(ctx, scripted):
    """The forked child gets running_skill and cannot re-invoke the same skill."""
    from wp5_fakes import mini_run_query, no_deps

    from harnesslab.agents.launcher import SubagentLauncher
    from harnesslab.model.scripted import call, say, turn

    L = SubagentLauncher(run_query=mini_run_query, make_deps=no_deps)
    ctx.skills = [Command("loop", "d", body="body", context="fork")]
    ctx.tools = [SkillTool(L)]
    scripted.add(turn(call("Skill", {"skill": "loop"})), say("ok, following the body instead"))
    out = SkillTool(L).call({"skill": "loop"}, tcx(ctx))
    assert out.data["status"] == "forked"
    child_req = scripted.requests[1]
    result = child_req.messages[-1]["content"][0]
    assert result["is_error"] and "already running" in str(result["content"])


# ------------------------------------------------------------------ MCP naming & hygiene (R16, R18, §4.3)


def test_mcp_names():
    assert normalize_server_name("my server.v2") == "my_server_v2"
    assert normalize_server_name("a  b..c", hosted=True) == "a_b_c"
    assert mcp_tool_name("git hub", "list_prs") == "mcp__git_hub__list_prs"


def test_flatten_and_api_accepts():
    s = {"oneOf": [{"type": "object", "properties": {"a": {"type": "string"}, "c": {"type": "string"}}, "required": ["a", "c"]}, {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["c"]}]}
    flat = flatten_combinators(s)
    assert flat["type"] == "object" and set(flat["properties"]) == {"a", "b", "c"} and flat["required"] == ["c"]
    assert api_accepts(flat) and not api_accepts(s) and not api_accepts({"type": "array"})
    assert not api_accepts({"type": "object", "properties": {"x": 3}})


def test_register_tools_drops_only_bad_schema_and_caps_descriptions():
    good = McpToolSpec("search", "<function_calls>x</function_calls>Find things " + "d" * 3000, {"type": "object", "properties": {"q": {"type": "string"}}})
    bad = McpToolSpec("broken", "bad", {"type": "string"})
    srv = InProcessMcpServer("docs", {"search": (good, lambda a: []), "broken": (bad, lambda a: [])})
    tools, degraded = register_tools(McpServerConfig("docs", "sdk"), srv)
    assert [t.name for t in tools] == ["mcp__docs__search"] and degraded == ["broken"]
    desc = tools[0].description()
    assert len(desc) == 2048 and "<function_calls>" not in desc
    assert tools[0].should_defer and tools[0].is_mcp
    tools2, _ = register_tools(McpServerConfig("docs", "sdk"), srv, env={"CLAUDE_AGENT_SDK_MCP_NO_PREFIX": "1", "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH": "10"})
    assert tools2[0].name == "search" and len(tools2[0].description()) == 10
    assert strip_invoke_markup("a<invoke name='x'>b</invoke>c") == "abc"


def test_managed_config_validation():
    ok = McpServerConfig("m", "http", url="https://example.com/mcp")
    assert validate_managed_config(ok) == []
    bad = McpServerConfig("m", "stdio", command="run ${HOME}", url="http://x")
    problems = " | ".join(validate_managed_config(bad))
    assert "http or sse" in problems and "https" in problems and "expansion" in problems
    assert validate_managed_config(McpServerConfig("m\x07", "http", url="https://x"))


def test_connection_plan_and_timeouts():
    servers = [McpServerConfig(f"l{i}", "stdio") for i in range(4)] + [McpServerConfig("r", "http", url="https://x"), McpServerConfig("must", "stdio", always_load=True)]
    blocking, background = connection_plan(servers)
    assert [[s.name for s in b] for b in blocking] == [["must"]]
    assert [[s.name for s in b] for b in background] == [["l0", "l1", "l2"], ["l3"], ["r"]]
    assert tool_timeouts(McpServerConfig("s", "stdio"), {}) == (100_000_000, 1_800_000)
    assert tool_timeouts(McpServerConfig("s", "http", timeout=5000), {}) == (5000, 300_000)
    assert tool_timeouts(McpServerConfig("s", "sdk", timeout=10), {"MCP_TOOL_TIMEOUT": "7000"}) == (7000, None)


# ------------------------------------------------------------------ output cap (R23, §4.5)


def text(n):
    return {"type": "text", "text": "x" * n}


def test_cap_output_bands():
    assert cap_output([text(400)], 200) == [text(400)]  # 100 tokens <= 0.5x
    big = cap_output([text(4000)], 200)  # 1000 tokens > 4x, text only: truncated without counting
    assert len(big[0]["text"]) == 800 and "paginat" in big[-1]["text"]
    counted = []
    mid = cap_output([text(600)], 200, count_tokens=lambda b: counted.append(1) or 150)
    assert counted and mid == [text(600)]  # middle band: precise count says it fits
    over = cap_output([text(600)], 200, count_tokens=lambda b: 999, footer="FOOT")
    assert over[-1]["text"] == "FOOT"


def test_cap_output_images_and_meta():
    img = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    out = cap_output([text(200), img, img], 1000)
    assert out[-1]["text"].startswith("[Output truncated") and any("omitted" in b.get("text", "") for b in out)
    meta = {"type": "text", "text": "hi", "_meta": {"blob": "m" * 70_000}}
    assert "_meta" not in cap_output([meta])[0]


def test_mcp_tool_call_maps_blocks_and_errors(ctx):
    def search(args):
        return [{"type": "text", "text": f"found {args['q']}"}, {"type": "image", "data": "AAA", "mimeType": "image/png"}]

    def broken(args):
        raise RuntimeError("server exploded")

    spec = McpToolSpec("search", "s", {"type": "object", "properties": {"q": {"type": "string"}}})
    srv = InProcessMcpServer("docs", {"search": (spec, search), "broken": (McpToolSpec("broken", "b", {"type": "object"}), broken)})
    tools, _ = register_tools(McpServerConfig("docs", "sdk"), srv)
    by = {t.name: t for t in tools}
    out = by["mcp__docs__search"].call({"q": "cats"}, tcx(ctx))
    assert isinstance(out.blocks[0], TextBlock) and out.blocks[0].text == "found cats" and isinstance(out.blocks[1], ImageBlock)
    err = by["mcp__docs__broken"].call({}, tcx(ctx))
    assert err.is_error and "server exploded" in err.data


def test_mcp_interrupted_call_unknown_outcome(ctx):
    release = threading.Event()
    spec = McpToolSpec("slow", "s", {"type": "object"})
    srv = InProcessMcpServer("s", {"slow": (spec, lambda a: release.wait(5) and [])})
    tool = register_tools(McpServerConfig("s", "sdk"), srv)[0][0]
    ctx.abort.abort("user")
    out = tool.call({}, tcx(ctx))
    release.set()
    assert out.is_error and out.data == UNKNOWN_OUTCOME_TEXT and "verify" in out.data.lower()


def test_mcp_auto_background(ctx):
    ctx.tasks, ctx.notifications = TaskRegistry(), NotificationQueue()
    release = threading.Event()
    spec = McpToolSpec("slow", "s", {"type": "object"})
    srv = InProcessMcpServer("s", {"slow": (spec, lambda a: (release.wait(5), [{"type": "text", "text": "late result"}])[1])})
    tool = register_tools(McpServerConfig("s", "sdk"), srv, env={"CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "40"})[0][0]
    out = tool.call({}, tcx(ctx))
    assert "background" in out.data and not out.is_error
    release.set()
    assert ctx.notifications.wait(5)
    assert "late result" in ctx.notifications.drain()[0].text()
