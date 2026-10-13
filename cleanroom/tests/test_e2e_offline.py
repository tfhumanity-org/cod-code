"""End-to-end offline scenarios through the real ``session.Engine`` (01#T12).

Every collaborator is the real one (tools, permission pipeline, hooks runner,
attachments, compaction, transcript, launcher, skills); only the model is a
``ScriptedModel``. These tests are the integration contract between the five
work packages - when one of them fails, a seam between packages broke.

Scenarios
---------
1. read -> edit -> bash in default mode: read-only tools run without a prompt,
   the edit asks, the human prompter (auto-answered) approves; one reminder
   envelope per attachment; transcript written.
2. ``rm -rf ~`` is never run, even in bypass mode (04 R16, R19): the
   bypass-immune ``dangerousRemoval`` check still asks, the prompt defaults to
   "no", and a headless session denies it outright. The denial guidance
   appears exactly once.
3. Context fills -> auto-compaction fires -> boundary + summary rows are in
   the transcript and a resumed engine starts from them (05 R22, §4.7).
4. A subagent spawned through the Agent tool returns its answer with a
   continuation handle, and SendMessage continues it (06 R13, 06#T2).
5. A project skill is listed to the model and invoked inline (07 R1, R8, 02 R37).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

from harnesslab.model.scripted import ScriptedModel, call, say, turn
from harnesslab.permissions.types import PermissionAnswer, PermissionMode
from harnesslab.session import Engine, create_session_context
from harnesslab.types import MessageEvent, SystemNoticeEvent, TerminalReason, Usage
from harnesslab.util.text import REMINDER_OPEN

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell required")


def tool_results(events) -> list[str]:
    return [b.text() for e in events if isinstance(e, MessageEvent) for b in e.message.tool_results()]


def request_texts(request) -> list[str]:
    """Every text-ish string the model saw in ``request.messages``."""
    out: list[str] = []
    for m in request.messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
            continue
        for b in content:
            if b.get("type") == "text":
                out.append(b["text"])
            elif b.get("type") == "tool_result":
                c = b.get("content")
                out.append(c if isinstance(c, str) else "".join(x.get("text", "") for x in c or []))
    return out


def transcript_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- 1. read -> edit -> bash


@posix_only
def test_read_edit_bash_round_trip(project, config_dir):
    target = project / "hello.txt"
    target.write_text("hello world\n")
    model = ScriptedModel([
        turn("Looking around.", call("Glob", {"pattern": "*.txt"}, id="g1")),
        turn("Reading.", call("Read", {"file_path": str(target)}, id="r1")),
        turn("Editing.", call("Edit", {"file_path": str(target), "old_string": "world", "new_string": "there"}, id="e1")),
        turn("Checking.", call("Bash", {"command": f"cat {target}", "description": "show file"}, id="b1")),
        say("All done."),
    ])
    asked: list[str] = []

    def prompter(req):
        asked.append(req.tool_name)
        return PermissionAnswer("allow")

    ctx = create_session_context(model, cwd=project, config_dir=config_dir, permission_mode=PermissionMode.DEFAULT, prompter=prompter)
    eng = Engine(ctx)
    events = eng.run("change world to there")

    assert eng.last_reason == TerminalReason.COMPLETED
    assert target.read_text() == "hello there\n"
    # 04#T4: Glob / Read / a read-only `cat` need no prompt; the edit does (04 §2.1 default mode)
    assert asked == ["Edit"]
    results = tool_results(events)
    assert len(results) == 4 and "hello there" in results[-1]
    # 02 R30: every reminder is wrapped exactly once
    for text in request_texts(model.requests[-1]):
        assert text.count(REMINDER_OPEN) <= 1, text
    # the agent's own edit is not reported back as an external change
    assert not any("was changed after you read it" in t for t in request_texts(model.requests[-1]))
    assert ctx.transcript_path.exists()
    # the loop published the seams other packages read (WORKPLAN change requests)
    assert ctx.extra["conversation_items"] and ctx.extra["cache_safe_params"]["system"]


# ---------------------------------------------------------------- 2. rm -rf ~ never runs


@posix_only
@pytest.mark.parametrize("prompter_kind", ["human_says_default", "headless"])
def test_dangerous_removal_denied_even_in_bypass(project, config_dir, prompter_kind):
    fake_home = project / "home"
    fake_home.mkdir()
    (fake_home / "precious.txt").write_text("keep me")
    model = ScriptedModel([
        turn("Cleaning up.", call("Bash", {"command": "rm -rf ~", "description": "clean"}, id="b1")),
        say("I could not do that."),
    ])
    seen = []

    def prompter(req):  # a human pressing Enter: takes the default the UI offers
        seen.append(req)
        return PermissionAnswer("deny" if req.default_to_no else "allow", message="Declined.")

    ctx = create_session_context(
        model, cwd=project, config_dir=config_dir, env={"HOME": str(fake_home)},
        permission_mode=PermissionMode.BYPASS,
        prompter=prompter if prompter_kind == "human_says_default" else None,
    )
    if prompter_kind == "headless":
        ctx.is_interactive = False
        ctx.permissions.headless = True
    eng = Engine(ctx)
    events = eng.run("tidy my home directory")

    assert (fake_home / "precious.txt").exists()
    [result] = tool_results(events)
    assert "was not run" in result
    assert result.count("do not try to get around this decision") == 1  # guidance appended once
    if prompter_kind == "human_says_default":
        # bypass-immune safety check still reaches a human, defaulting to "no" (04 R19)
        assert len(seen) == 1 and seen[0].default_to_no
    assert eng.last_reason == TerminalReason.COMPLETED


# ---------------------------------------------------------------- 3. auto-compaction + resume


def test_auto_compaction_writes_boundary_and_resume_starts_there(project, config_dir):
    big = Usage(input_tokens=60_000, output_tokens=50)
    summary = "<analysis>scratch</analysis><summary>1. The user is tuning a parser. 2. Next: add tests.</summary>"
    model = ScriptedModel([
        say("Noted, working on the parser.", usage=big),  # turn 1 fills the (tiny) window
        say(summary),                                      # the compaction summarizer's call
        say("Continuing after compaction."),               # turn 2 main call
    ])
    # threshold = 20% of the effective window (36k): above the system-prompt prefix
    # (else the prefix-too-large breaker rightly refuses, 05 R12), below turn 1's usage
    env = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "20"}
    ctx = create_session_context(model, cwd=project, config_dir=config_dir, env=env, prompter=lambda r: PermissionAnswer("allow"))
    eng = Engine(ctx)
    eng.run("let's tune the parser")
    events = eng.run("keep going")

    notices = [e for e in events if isinstance(e, SystemNoticeEvent) and e.subtype == "compact_boundary"]
    assert notices and notices[0].data["trigger"] == "auto"
    assert eng.last_reason == TerminalReason.COMPLETED
    # the summarizer request was a compact query; the next main request starts from the summary
    assert model.requests[1].query_source == "compact"
    assert any("tuning a parser" in t for t in request_texts(model.requests[2]))

    rows = transcript_rows(ctx.transcript_path)
    boundary = [r for r in rows if r.get("subtype") == "compact_boundary"]
    assert boundary and boundary[-1].get("parentUuid", boundary[-1].get("parent_uuid")) is None
    assert any(r.get("isCompactSummary") or r.get("is_compact_summary") for r in rows)

    # resume: a fresh engine on the same session id loads from the boundary onwards
    model2 = ScriptedModel([say("Resumed.")])
    ctx2 = create_session_context(model2, cwd=project, config_dir=config_dir, env=env)
    ctx2.session_id = ctx.session_id
    resumed = Engine.resume(ctx2)
    assert any(getattr(it, "is_compact_summary", False) for it in resumed.items)
    assert not any(getattr(it, "text", lambda: "")() == "let's tune the parser" for it in resumed.items)
    resumed.run("are you there?")
    assert resumed.last_reason == TerminalReason.COMPLETED
    assert any("tuning a parser" in t for t in request_texts(model2.requests[-1]))


# ---------------------------------------------------------------- 4. subagent via the Agent tool


def test_subagent_returns_result_with_continuation_handle(project, config_dir):
    def follow_up(request):
        """Read the continuation handle out of the Agent result the model just got."""
        handle = re.search(r"agentId: (\S+)", "\n".join(request_texts(request))).group(1)
        return turn("Asking a follow-up.", call("SendMessage", {"to": handle, "message": "And directories?"}, id="s1"))

    model = ScriptedModel([
        turn("Delegating.", call("Agent", {
            "description": "count files", "prompt": "Count the files and report.",
            "subagent_type": "general-purpose", "run_in_background": False,
        }, id="a1")),
        say("There are 3 files."),  # the child's only turn
        follow_up,
        # SendMessage resumes the child in the background (06 R28): the parent's last
        # round and the child's continuation race for these two identical turns.
        say("Done."),
        say("Done."),
    ])
    ctx = create_session_context(model, cwd=project, config_dir=config_dir, prompter=lambda r: PermissionAnswer("allow"))
    eng = Engine(ctx)
    events = eng.run("how many files?")

    results = tool_results(events)
    assert "There are 3 files." in results[0] and "agentId:" in results[0]
    handle = re.search(r"agentId: (\S+)", results[0]).group(1)
    assert handle in results[1] and "resumed" in results[1]
    assert eng.last_reason == TerminalReason.COMPLETED
    # the child ran with the agent prompt + notes block (06 R14), not the main prompt
    child_system = " ".join(b.text for b in model.requests[1].system)
    assert "Notes for this run" in child_system

    # the resumed child finishes and reports through the single notification queue
    deadline = time.monotonic() + 5
    while ctx.tasks.running_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ctx.tasks.running_count() == 0
    note = "\n".join(m.text() for m in ctx.notifications.drain())
    assert handle in note and "Done." in note
    # the continuation saw the child's earlier exchange (06#T2: resume from its transcript)
    child_requests = [r for r in model.requests if any("And directories?" in t for t in request_texts(r))]
    assert child_requests and any("Count the files" in t for t in request_texts(child_requests[0]))


# ---------------------------------------------------------------- 5. skills


@posix_only
def test_skill_is_listed_invoked_and_its_grant_expires(project, config_dir):
    skill_dir = project / ".claude" / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Greet the user warmly\nallowed-tools: Bash(touch:*)\n---\n"
        "Say hello to $ARGUMENTS in one sentence, then touch greeted.txt.\n"
    )
    model = ScriptedModel([
        turn("Using the skill.", call("Skill", {"skill": "greet", "args": "Ada"}, id="k1")),
        turn("Marking it.", call("Bash", {"command": f"touch {project / 'greeted.txt'}", "description": "mark"}, id="b1")),
        say("Hello, Ada!"),
        # next user message: the skill's grant has expired, so touch must ask again
        turn("Again.", call("Bash", {"command": f"touch {project / 'again.txt'}", "description": "mark"}, id="b2")),
        say("Done."),
    ])
    asked: list[str] = []

    def prompter(req):
        asked.append(f"{req.tool_name}:{req.input.get('command', req.input.get('skill'))}")
        return PermissionAnswer("allow")

    ctx = create_session_context(model, cwd=project, config_dir=config_dir, prompter=prompter)
    assert [s.name for s in ctx.skills] == ["greet"]
    eng = Engine(ctx)
    events = eng.run("greet Ada")

    first = request_texts(model.requests[0])
    assert any("greet: Greet the user warmly" in t for t in first)  # 02 R37 listing
    results = tool_results(events)
    assert "greet" in results[0]
    later = request_texts(model.requests[1])
    assert any("Say hello to Ada" in t for t in later)  # inline expansion (07 R8)
    assert [s["name"] for s in ctx.invoked_skills] == ["greet"]
    assert (project / "greeted.txt").exists()
    assert asked == ["Skill:greet"]  # the skill's allowed-tools layer covered `touch`
    assert eng.last_reason == TerminalReason.COMPLETED

    eng.run("once more")
    assert (project / "again.txt").exists()
    assert asked[-1].startswith("Bash:touch")  # layer expired at the new user message
