"""WP5 - workflow runtime with chained-key journal (spec 06 R34-R42, §4.5)."""

from __future__ import annotations

import re
import threading

import pytest
from wp5_fakes import mini_run_query, no_deps

from harnesslab.agents.launcher import SubagentLauncher
from harnesslab.agents.workflow import (
    MAX_AGENT_CALLS,
    ROOT_KEY,
    AgentCapError,
    BudgetExceeded,
    Journal,
    NarrowingError,
    PrefixStagger,
    StructuredOutputTool,
    UsageLimitError,
    WorkflowError,
    WorkflowRun,
    chain_key,
    journal_path,
    make_launcher_runner,
    run_workflow,
    workflow_concurrency,
)
from harnesslab.model.scripted import call, say, turn
from harnesslab.tools.base import ToolCallContext


class CountingRunner:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.prompts: list[str] = []
        self.fail_on = fail_on or set()
        self.lock = threading.Lock()

    def __call__(self, prompt, opts):
        with self.lock:
            self.prompts.append(prompt)
        if prompt in self.fail_on:
            raise RuntimeError(f"boom {prompt}")
        return f"out:{prompt}"


def three_step(run, args):
    a = run.agent("plan " + args["topic"])
    b = run.agent("build " + a)
    return run.agent("check " + b)


def test_concurrency_formula():
    assert [workflow_concurrency(c) for c in (1, 4, 8, 18, 64)] == [2, 2, 6, 16, 16]


def test_chain_keys_encode_prefix():
    k1 = chain_key("a", {}, ROOT_KEY)
    assert chain_key("b", {}, k1) != chain_key("b", {}, chain_key("x", {}, ROOT_KEY))
    assert chain_key("a", {"model": "m", "label": "l"}, ROOT_KEY) == chain_key("a", {"label": "l", "model": "m"}, ROOT_KEY)


def test_run_and_resume_replays_longest_unchanged_prefix(ctx):
    r1 = CountingRunner()
    assert run_workflow(ctx, three_step, {"topic": "t"}, runner=r1) == "out:check out:build out:plan t"
    first_id = ctx.extra["last_workflow_run_id"]
    assert re.match(r"^wf_[a-z0-9-]{6,}$", first_id) and journal_path(ctx, first_id).exists()
    # resume unchanged: nothing runs live
    r2 = CountingRunner()
    assert run_workflow(ctx, three_step, {"topic": "t"}, runner=r2, resume_from=first_id) == "out:check out:build out:plan t"
    assert r2.prompts == []

    # change the 2nd call: 1st replayed, 2nd and 3rd live (even though 3rd's prompt would hit by itself)
    def changed(run, args):
        a = run.agent("plan " + args["topic"])
        b = run.agent("build " + a, label="v2")
        return run.agent("check " + b)

    r3 = CountingRunner()
    run_workflow(ctx, changed, {"topic": "t"}, runner=r3, resume_from=first_id)
    assert r3.prompts == ["build out:plan t", "check out:build out:plan t"]
    # the resumed run's journal is complete on its own
    second = ctx.extra["last_workflow_run_id"]
    r4 = CountingRunner()
    run_workflow(ctx, changed, {"topic": "t"}, runner=r4, resume_from=second)
    assert r4.prompts == []


def test_resume_bad_id_refused(ctx):
    with pytest.raises(WorkflowError, match="invalid run id"):
        run_workflow(ctx, three_step, {"topic": "t"}, runner=CountingRunner(), resume_from="run-1")
    with pytest.raises(WorkflowError, match="no journal"):
        run_workflow(ctx, three_step, {"topic": "t"}, runner=CountingRunner(), resume_from="wf_abcdef")


def test_parallel_and_pipeline_failures_become_none(ctx):
    runner = CountingRunner(fail_on={"b", "stage2:y"})

    def wf(run, args):
        par = run.parallel([lambda p=p: run.agent(p) for p in ("a", "b", "c")])
        pipe = run.pipeline(["x", "y"], lambda i: run.agent(f"stage1:{i}") and i, lambda i: run.agent(f"stage2:{i}"))
        return par, pipe

    par, pipe = run_workflow(ctx, wf, {}, runner=runner)
    assert par == ["out:a", None, "out:c"]
    assert pipe == ["out:stage2:x", None]


def test_parallel_resume_is_per_branch(ctx):
    def wf(run, args):
        return run.parallel([lambda p=p: run.agent(p) for p in args["items"]])

    run_workflow(ctx, wf, {"items": ["a", "b", "c"]}, runner=CountingRunner())
    rid = ctx.extra["last_workflow_run_id"]
    r2 = CountingRunner()
    out = run_workflow(ctx, wf, {"items": ["a", "B", "c"]}, runner=r2, resume_from=rid)
    assert out == ["out:a", "out:B", "out:c"] and r2.prompts == ["B"]


def test_failed_agent_journaled_and_raised(ctx):
    runner = CountingRunner(fail_on={"x"})
    with pytest.raises(RuntimeError):
        run_workflow(ctx, lambda run, a: run.agent("x"), {}, runner=runner)
    j = Journal.load(journal_path(ctx, ctx.extra["last_workflow_run_id"]))
    assert [e.kind for e in j.entries] == ["started", "failed"]


def test_call_cap(ctx):
    run = WorkflowRun("wf_test01", Journal(), lambda p, o: p)
    run.calls = MAX_AGENT_CALLS
    with pytest.raises(AgentCapError):
        run.agent("one more")
    run2 = WorkflowRun("wf_test02", Journal(), lambda p, o: p)
    assert run2.parallel([lambda: run2.agent("ok")]) == ["ok"]


def test_budget_cap():
    run = WorkflowRun("wf_test03", Journal(), lambda p, o: p, budget_tokens=100)
    run.charge(150)
    with pytest.raises(BudgetExceeded):
        run.agent("x")


@pytest.mark.parametrize(
    "opts",
    [
        {"bashCommandClamp": ["git status"]},
        {"bashCommandClamp": []},
        {"disallowedTools": "Bash"},
        {"isolation": "remote"},
        {"schema": "nope"},
        {"stallMs": -1},
        {"colour": "red"},
    ],
)
def test_malformed_narrowing_refused(opts):
    ran = []
    run = WorkflowRun("wf_test04", Journal(), lambda p, o: ran.append(p))
    with pytest.raises(NarrowingError):
        run.agent("x", **opts)
    assert ran == [] and run.calls == 0


def test_usage_limit_waits_at_most_twice():
    attempts, sleeps = [], []

    def runner(p, o):
        attempts.append(p)
        raise UsageLimitError()

    run = WorkflowRun("wf_test05", Journal(), runner, sleep=sleeps.append, jitter=lambda a, b: (a + b) / 2)
    with pytest.raises(UsageLimitError):
        run.agent("x")
    assert len(attempts) == 3 and sleeps == [60.0, 60.0]


def test_prefix_stagger_leader_follower():
    t = [0.0]
    s = PrefixStagger(wait_ms=10, warm_s=270, clock=lambda: t[0])
    assert s.enter("k") == "leader"
    assert s.enter("k") == "follower"  # waited (up to 10 ms) for the leader
    s.first_response("k")
    assert s.enter("k") == "warm"
    t[0] = 300
    assert s.enter("k") == "leader"


def test_structured_output_tool_once(ctx):
    tool = StructuredOutputTool({"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
    cc = ToolCallContext(ctx, "t", ctx.abort.signal)
    assert tool.call({"n": "x"}, cc).is_error
    assert not tool.call({"n": 3}, cc).is_error and tool.value == {"n": 3}
    assert tool.call({"n": 4}, cc).is_error and tool.value == {"n": 3}


def test_launcher_runner_structured_output(ctx, scripted):
    scripted.add(turn(call("StructuredOutput", {"verdict": "ok"})), say("done"), say("plain text result"))
    L = SubagentLauncher(run_query=mini_run_query, make_deps=no_deps)
    runner = make_launcher_runner(ctx, L)
    assert runner("judge it", {"schema": {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}}) == {"verdict": "ok"}
    assert runner("say something", {}) == "plain text result"
    child = scripted.requests[0]
    assert any(t["name"] == "StructuredOutput" for t in child.tools)
