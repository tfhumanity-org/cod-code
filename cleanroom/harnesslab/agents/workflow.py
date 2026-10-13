"""Deterministic workflow runtime with a chained-key journal (WP5).

Implements spec 06 §2.6, R34-R42 and §4.5; takeaways 06#T7 (orchestration in
deterministic code; failures resolve to None; prefer pipelines over
barriers), 06#T8 (structured output as a forced tool call), 06#T9 (stagger
same-prefix agents); P24.

Design choice (spec describes a JS script runtime): workflows here are *Python
callables* ``fn(run: WorkflowRun, args) -> Any`` that may only use the ``run``
API (``agent``, ``parallel``, ``pipeline``, ``phase``, ``log``, ``budget``). Wall
clock and randomness are blocked by convention and by ``WorkflowRun`` exposing
no such API; the journal makes replay exact.

* Journal key ``key_i = H(prompt_i, canonical(opts_i), key_{i-1})`` - resume
  returns journaled results until the first miss, then everything runs live
  (longest unchanged prefix, R40).
* Caps: concurrency ``min(16, max(2, cpus - 2))``; 1000 ``agent()`` calls; a
  token budget; ``parallel``/``pipeline`` never raise, failed items -> None (R39).
* ``agent(prompt, schema=...)`` gives the child a ``StructuredOutput`` tool that
  must be called exactly once (R38). Malformed narrowing options refuse (R37).
* Usage-limit errors: wait and re-run, <= 2 waits per run, 30-90 s jitter (R41).
* Run ids ``wf_`` + ``[a-z0-9-]{6,}`` (R35).

Design choices (where Python threads differ from the JS event loop)
-------------------------------------------------------------------
* **Branch-local key chains.** In JS, ``agent()`` calls inside ``parallel`` are
  keyed in call order before any await. With threads the order is not stable,
  so every ``parallel``/``pipeline`` item gets its own chain seeded with
  ``H("parallel[i]", {}, parent_key)``; after the join the parent chain advances
  by ``H("join", {"n": n}, parent_key)``. A branch "goes live" on its own first
  miss, and the parent goes live if any branch did. Resume semantics are thus
  "longest unchanged prefix" per branch.
* **Prefix stagger (R42)** is implemented in ``PrefixStagger`` with an injectable
  clock; the leader for a prefix key runs at once, followers wait up to
  ``CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS`` (5000) for the leader's first
  response, and the key stays warm for 270 s.
* **Item cap** per ``parallel``/``pipeline`` call: ``MAX_PARALLEL_ITEMS`` = 500
  (06 §8: the real value was not resolved).
* ``run_workflow`` keeps finished runs in ``ctx.extra["workflow_runs"]`` and the
  journal at ``<session_state_dir>/workflows/<run_id>.jsonl``. Scripts are Python
  callables, so the "persist the script" half of R35 does not apply.
* Deferred: the ``Workflow`` *tool* itself (opt-in gating R34 lives in prompts /
  permissions), nested ``workflow()`` (one level).
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from harnesslab.config import env_int
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput
from harnesslab.util.schema import validate as validate_schema

MAX_AGENT_CALLS = 1000
MAX_PARALLEL_ITEMS = 500
MAX_USAGE_WAITS = 2
USAGE_WAIT_JITTER_S = (30.0, 90.0)
PREFIX_STAGGER_MS = 5000
PREFIX_WARM_S = 270.0
RUN_ID_PATTERN = r"^wf_[a-z0-9-]{6,}$"
ROOT_KEY = "root"

AGENT_OPTION_KEYS = frozenset(
    {"label", "phase", "schema", "model", "effort", "isolation", "agentType", "disallowedTools", "bashCommandClamp", "stallMs"}
)
_BASH_PATTERN = re.compile(r"^Bash\(.+\)$")


class WorkflowError(Exception):
    """Base class; the message is meant for the model / script author."""


class NarrowingError(WorkflowError):
    """Malformed narrowing options: the spawn is refused, never run un-narrowed (R37)."""


class AgentCapError(WorkflowError):
    """More than ``MAX_AGENT_CALLS`` agent() calls in one run (R39)."""


class BudgetExceeded(WorkflowError):
    """The run's token budget is used up (R39)."""


class UsageLimitError(Exception):
    """Raised by a runner when the account hit a usage limit (R41)."""


def workflow_concurrency(cpus: int) -> int:
    """``min(16, max(2, cpus - 2))`` concurrent agents per run (R39)."""
    return min(16, max(2, cpus - 2))


def chain_key(prompt: str, opts: dict[str, Any], prev_key: str) -> str:
    """Deterministic chained key (FINAL helper: pure function, safe to implement now)."""
    canon = json.dumps(opts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_key}\x00{prompt}\x00{canon}".encode()).hexdigest()[:32]


def validate_narrowing(opts: dict[str, Any]) -> None:
    """Refuse malformed ``agent()`` options instead of running un-narrowed (R37)."""
    unknown = set(opts) - AGENT_OPTION_KEYS
    if unknown:
        raise NarrowingError(f"unknown agent() option(s): {', '.join(sorted(unknown))}")
    clamp = opts.get("bashCommandClamp")
    if clamp is not None:
        if not isinstance(clamp, list) or not clamp or not all(isinstance(c, str) and _BASH_PATTERN.match(c) for c in clamp):
            raise NarrowingError("bashCommandClamp must be a non-empty list of 'Bash(<pattern>)' entries")
    dis = opts.get("disallowedTools")
    if dis is not None and (not isinstance(dis, list) or not all(isinstance(t, str) and t for t in dis)):
        raise NarrowingError("disallowedTools must be a list of tool names")
    if opts.get("isolation") not in (None, "worktree"):
        raise NarrowingError("isolation may only be 'worktree'")
    if "schema" in opts and not isinstance(opts["schema"], dict):
        raise NarrowingError("schema must be a JSON-schema object")
    stall = opts.get("stallMs")
    if stall is not None and (not isinstance(stall, int) or isinstance(stall, bool) or stall <= 0):
        raise NarrowingError("stallMs must be a positive integer")


@dataclass
class JournalEntry:
    key: str
    kind: Literal["started", "result", "failed"]
    value: Any = None


class Journal:
    """Append-only JSONL journal for one run.

    ``prior`` is the journal of the run being resumed: lookups fall back to it, and
    replayed hits are re-appended so the new journal is complete on its own.
    """

    def __init__(self, path: Path | None = None, prior: "Journal | None" = None) -> None:
        self.path = path
        self.entries: list[JournalEntry] = []
        self.prior = prior
        self._lock = threading.Lock()

    def append(self, key: str, kind: Literal["started", "result", "failed"], value: Any = None) -> None:
        entry = JournalEntry(key, kind, value)
        with self._lock:
            self.entries.append(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": key, "kind": kind, "value": value}, default=str) + "\n")

    def get(self, key: str) -> JournalEntry | None:
        """Most useful entry for ``key``: a ``result`` if one exists, else the latest entry."""
        with self._lock:
            found = [e for e in self.entries if e.key == key]
        hit = next((e for e in reversed(found) if e.kind == "result"), None) or (found[-1] if found else None)
        if (hit is None or hit.kind != "result") and self.prior is not None:
            prior = self.prior.get(key)
            if prior is not None and (hit is None or prior.kind == "result"):
                return prior
        return hit

    @classmethod
    def load(cls, path: Path) -> "Journal":
        j = cls(path)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    j.entries.append(JournalEntry(d["key"], d["kind"], d.get("value")))
                except (ValueError, KeyError):
                    continue  # a torn last line from a crash is ignored
        return j


#: (prompt, opts) -> value. Production: runs a subagent via agents.launcher; tests: a lambda.
AgentRunner = Callable[[str, dict[str, Any]], Any]


class PrefixStagger:
    """One leader per prompt-prefix key; followers wait for its first response (R42, 06#T9)."""

    def __init__(self, wait_ms: int = PREFIX_STAGGER_MS, warm_s: float = PREFIX_WARM_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.wait_ms, self.warm_s, self.clock = wait_ms, warm_s, clock
        self._lock = threading.Lock()
        self._leaders: dict[str, threading.Event] = {}
        self._warm_until: dict[str, float] = {}

    def enter(self, key: str) -> str:
        """Returns ``"warm"``, ``"leader"`` or ``"follower"`` (after waiting)."""
        with self._lock:
            if self._warm_until.get(key, 0) > self.clock():
                return "warm"
            ev = self._leaders.get(key)
            if ev is None:
                self._leaders[key] = threading.Event()
                return "leader"
        ev.wait(self.wait_ms / 1000)
        return "follower"

    def first_response(self, key: str) -> None:
        with self._lock:
            self._warm_until[key] = self.clock() + self.warm_s
            ev = self._leaders.pop(key, None)
        if ev is not None:
            ev.set()


def prefix_key(prompt: str, opts: dict[str, Any], n: int = 200) -> str:
    return chain_key(prompt[:n], {k: opts.get(k) for k in ("model", "agentType")}, "prefix")


@dataclass
class _Branch:
    prev_key: str
    live: bool


@dataclass
class WorkflowRun:
    run_id: str
    journal: Journal
    runner: AgentRunner
    concurrency: int = 4
    calls: int = 0
    live: bool = False
    prev_key: str = ROOT_KEY
    logs: list[str] = field(default_factory=list)
    budget_tokens: int | None = None
    tokens_used: int = 0
    phases: list[str] = field(default_factory=list)
    usage_waits: int = 0
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[float, float], float] = random.uniform
    stagger: PrefixStagger | None = None

    def __post_init__(self) -> None:
        self._sem = threading.Semaphore(max(1, self.concurrency))
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---- branch-local chain state (see module docstring)
    def _cursor(self) -> Any:
        return getattr(self._local, "cursor", None) or self

    def charge(self, tokens: int) -> None:
        """Runners report token usage here; the next agent() past the budget refuses."""
        with self._lock:
            self.tokens_used += max(0, int(tokens))

    def agent(self, prompt: str, **opts: Any) -> Any:
        """Run (or replay) one agent call. Raises on failure; ``parallel`` turns that into None."""
        validate_narrowing(opts)
        with self._lock:
            self.calls += 1
            if self.calls > MAX_AGENT_CALLS:
                raise AgentCapError(f"this workflow run already made {MAX_AGENT_CALLS} agent() calls; split the work into another run")
            if self.budget_tokens is not None and self.tokens_used >= self.budget_tokens:
                raise BudgetExceeded(f"token budget of {self.budget_tokens} reached; no more agents in this run")
        cur = self._cursor()
        key = chain_key(prompt, opts, cur.prev_key)
        cur.prev_key = key
        if not cur.live:
            hit = self.journal.get(key)
            if hit is not None and hit.kind == "result":
                if self.journal.prior is not None:
                    self.journal.append(key, "result", hit.value)
                return hit.value
            cur.live = True  # first miss: everything after runs live (R40)
        self.journal.append(key, "started")
        with self._sem:
            pk = prefix_key(prompt, opts)
            if self.stagger is not None:
                self.stagger.enter(pk)
            try:
                value = self._run_with_usage_retries(prompt, opts)
            except Exception as e:
                self.journal.append(key, "failed", str(e))
                raise
            finally:
                if self.stagger is not None:
                    self.stagger.first_response(pk)
        self.journal.append(key, "result", value)
        return value

    def _run_with_usage_retries(self, prompt: str, opts: dict[str, Any]) -> Any:
        while True:
            try:
                return self.runner(prompt, opts)
            except UsageLimitError:
                with self._lock:
                    if self.usage_waits >= MAX_USAGE_WAITS:
                        raise
                    self.usage_waits += 1
                self.sleep(self.jitter(*USAGE_WAIT_JITTER_S))

    def _fork_branches(self, n: int) -> tuple[Any, list[_Branch]]:
        parent = self._cursor()
        branches = [_Branch(chain_key(f"parallel[{i}]", {}, parent.prev_key), parent.live) for i in range(n)]
        return parent, branches

    def _join(self, parent: Any, branches: list[_Branch]) -> None:
        parent.prev_key = chain_key("join", {"n": len(branches)}, parent.prev_key)
        if any(b.live for b in branches):
            parent.live = True

    def _run_branch(self, branch: _Branch, fn: Callable[[], Any]) -> Any:
        self._local.cursor = branch
        try:
            return fn()
        finally:
            self._local.cursor = None

    def parallel(self, thunks: list[Callable[[], Any]]) -> list[Any]:
        """Run thunks concurrently; a failed thunk yields ``None``; never raises (R39)."""
        if len(thunks) > MAX_PARALLEL_ITEMS:
            raise WorkflowError(f"parallel() takes at most {MAX_PARALLEL_ITEMS} items")
        if not thunks:
            return []
        parent, branches = self._fork_branches(len(thunks))
        results: list[Any] = [None] * len(thunks)
        with cf.ThreadPoolExecutor(max_workers=len(thunks)) as pool:
            futs = {pool.submit(self._run_branch, branches[i], t): i for i, t in enumerate(thunks)}
            for f in cf.as_completed(futs):
                try:
                    results[futs[f]] = f.result()
                except Exception:  # noqa: BLE001 - settled, not rejected
                    results[futs[f]] = None
        self._join(parent, branches)
        return results

    def pipeline(self, items: list[Any], *stages: Callable[[Any], Any]) -> list[Any]:
        """Each item flows through all stages independently - no barrier between stages."""

        def chain(item: Any) -> Callable[[], Any]:
            def run() -> Any:
                value = item
                for stage in stages:
                    value = stage(value)
                return value

            return run

        return self.parallel([chain(i) for i in items])

    def phase(self, name: str) -> None:
        self.phases.append(name)
        self.log(f"== phase: {name}")

    def log(self, text: str) -> None:
        self.logs.append(text)

    @property
    def budget(self) -> dict[str, int | None]:
        return {"total": self.budget_tokens, "used": self.tokens_used}


def new_run_id() -> str:
    return "wf_" + secrets.token_hex(4)


def journal_path(ctx: SessionContext, run_id: str) -> Path:
    return ctx.session_state_dir / "workflows" / f"{run_id}.jsonl"


def run_workflow(
    ctx: SessionContext,
    fn: Callable[[WorkflowRun, dict[str, Any]], Any],
    args: dict[str, Any],
    *,
    runner: AgentRunner,
    resume_from: str | None = None,
    budget_tokens: int | None = None,
    cpus: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Execute ``fn`` with a fresh ``WorkflowRun``; optionally resume a previous run (R35, R40)."""
    prior = None
    if resume_from is not None:
        if not re.match(RUN_ID_PATTERN, resume_from):
            raise WorkflowError(f"invalid run id {resume_from!r}: expected 'wf_' followed by at least 6 of [a-z0-9-]")
        prev_path = journal_path(ctx, resume_from)
        if not prev_path.exists():
            raise WorkflowError(f"no journal found for run {resume_from}")
        prior = Journal.load(prev_path)
    run_id = new_run_id()
    stagger = PrefixStagger(env_int(ctx.env, "CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS", PREFIX_STAGGER_MS, lo=0))
    run = WorkflowRun(
        run_id=run_id,
        journal=Journal(journal_path(ctx, run_id), prior=prior),
        runner=runner,
        concurrency=workflow_concurrency(cpus if cpus is not None else (os.cpu_count() or 4)),
        budget_tokens=budget_tokens,
        sleep=sleep,
        stagger=stagger,
    )
    ctx.extra.setdefault("workflow_runs", {})[run_id] = run
    ctx.extra["last_workflow_run_id"] = run_id
    return fn(run, args)


# ------------------------------------------------------------------ structured output (R38, 06#T8)


class StructuredOutputTool(Tool):
    """Forced output channel for schema-bearing workflow agents: call exactly once."""

    name = "StructuredOutput"
    search_hint = "return the final structured result"

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema
        self.input_schema = schema  # type: ignore[misc]
        self.value: Any = None
        self.called = False

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        return "Return your final result by calling this tool exactly once with data matching its schema."

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        if self.called:
            return ToolOutput(data="StructuredOutput was already called; your result is recorded. Stop now.", is_error=True)
        errors = validate_schema(input, self.schema)
        if errors:
            return ToolOutput(data="The result does not match the schema; fix it and call again:\n" + "\n".join(errors), is_error=True)
        self.value, self.called = input, True
        return ToolOutput(data="Result recorded. You are done; end your turn.")


WORKFLOW_AGENT_DISALLOWED = ["Agent", "Workflow", "SendMessage", "AskUserQuestion"]


def make_launcher_runner(ctx: SessionContext, launcher: Any) -> AgentRunner:
    """Production ``AgentRunner``: each agent() call is a synchronous subagent (R36-R38).

    Workflow agents cannot spawn agents or message the user (06 R25). With
    ``schema`` the child gets a ``StructuredOutput`` tool and its recorded value is
    returned; otherwise the final text is returned verbatim.
    """
    from harnesslab.agents.definitions import AgentDefinition
    from harnesslab.agents.launcher import AgentInput, DEFAULT_MAX_DEPTH, _AgentRecord

    def run(prompt: str, opts: dict[str, Any]) -> Any:
        base = launcher._resolve(ctx, opts.get("agentType") or "general-purpose")
        definition = AgentDefinition(
            name=base.name,
            description=base.description,
            prompt=base.prompt,
            tools=base.tools,
            disallowed_tools=list(base.disallowed_tools) + WORKFLOW_AGENT_DISALLOWED + list(opts.get("disallowedTools") or []),
            model=opts.get("model") or base.model,
            continuable=False,
        )
        extra: list[Tool] = []
        so = None
        if isinstance(opts.get("schema"), dict):
            so = StructuredOutputTool(opts["schema"])
            extra.append(so)
            prompt = prompt + "\n\nWhen finished, call the StructuredOutput tool exactly once with your result."
        inp = AgentInput(description=opts.get("label") or "workflow agent", prompt=prompt, model=opts.get("model"), effort=opts.get("effort"), run_in_background=False)
        child, items, writer, _ = launcher._build_child(ctx, definition, inp, [], is_async=False, is_teammate=False, max_depth=DEFAULT_MAX_DEPTH, extra_tools=extra)
        if opts.get("bashCommandClamp"):
            child.extra["bash_command_clamp"] = list(opts["bashCommandClamp"])
        record = _AgentRecord(child.agent_id or "", definition, child, ctx, items, inp.description, writer=writer)
        launcher._register(record, None)
        launcher._inc(ctx.session_id)
        record.running = True
        result = launcher._execute(record)
        if so is not None:
            if not so.called:
                raise WorkflowError("the agent finished without calling StructuredOutput")
            return so.value
        return result.content

    return run
