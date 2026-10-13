# 06 — Multi-agent & background machinery (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

This document specifies how a coding-agent harness delegates work to nested agents and runs work in the background:

- the `Agent` tool (alias `Task`): input/output contract, launch checks, sync vs async, model and permission inheritance;
- subagent context construction and role-based tool filtering;
- agent definition files and built-in agent types;
- cache-identical forks;
- the unified background notification queue, continuation via `SendMessage`, `TaskStop`, and coordinator mode;
- experimental teams/teammates and cross-session peers;
- the `Workflow` tool: a deterministic script runtime with journaled resume;
- background shell commands, MCP auto-backgrounding, and tool detach;
- `Monitor`, `CronCreate`/`CronDelete`/`CronList`, `ScheduleWakeup` and `/loop`;
- worktree isolation (`EnterWorktree`/`ExitWorktree`) and agent worktree cleanup.

---

## 2. Concepts & data model

### 2.1 Core primitive

Everything is built on **one nested query loop** registered in a per-session **task registry**. Its output reaches the owner either:
- synchronously, as the tool result of the call that started it; or
- asynchronously, as a user-role **task notification** queued for the owner's next turn.

Background shells, monitors, MCP auto-background, workflows, cron fires and loop wakeups use the same queue.

### 2.2 Agent tool input

```
AgentInput {
  description: str              # 3–5 words
  prompt: str
  subagent_type?: str           # default: general-purpose; "fork" for forks
  model?: "sonnet" | "opus" | "haiku" | ...   # ignored for forks / when forced by env
  effort?: level                # only when the user or project instructions ask
  run_in_background?: bool      # background is the default
  name?: str                    # makes the agent addressable; regex-checked;
                                # reserved: main, team-lead, user, system, id-shaped strings
  isolation?: "worktree" | "remote"
  cwd?: str                     # host-only; exclusive with worktree; hidden from model schema
  team_name?, mode?             # deprecated, ignored
}
```

### 2.3 Agent tool output (tagged union)

```
completed        { content, usage, totalTokens, toolStats, worktreePath?, worktreeBranch?, canContinueAgent }
async_launched   { agentId, outputFile, canReadOutputFile, sharesCwd }
remote_launched  { taskId, sessionUrl }
teammate_spawned { ... }
```

Tool properties: read-only = true, concurrency-safe = true, max result size 100,000 chars.

### 2.4 Task notification (user-role message, wrapped as a system reminder)

```
<task-notification>
  <task-id>…</task-id>
  <status>completed | failed | killed | blocked</status>
  <summary>…</summary>
  <result>…</result>
  <usage>…</usage>
</task-notification>
```
Queued with mode `task-notification`, priority `next`.

### 2.5 Agent definition (`.claude/agents/*.md` frontmatter)

| key | semantics |
|---|---|
| `name`, `description` | required. Name must not start with `-`, contain `:`, or exceed a length cap. Escaped newlines in description are unescaped |
| `tools`, `disallowedTools` | lists; `Agent(a,b)` restricts which agent types this agent may spawn; `mcp__srv` / `mcp__srv__*` match a whole server; `*` or absent = all |
| `model` | `inherit`, alias, or full id |
| `effort` | level name or integer |
| `permissionMode` | default, plan, acceptEdits, bypassPermissions, dontAsk, auto, bubble |
| `maxTurns` | positive int |
| `skills` | skill names to preload |
| `initialPrompt` | string |
| `memory` | `user` / `project` / `local`; auto-adds Read/Write/Edit and a memory section |
| `background` | always async |
| `omitClaudeMd` | drop project instruction files (managed instructions kept) |
| `isolation` | `worktree` / `remote` |
| `mcpServers` | agent-scoped servers (reserved names and some transports rejected) |
| `hooks` | per-agent hooks; a top-level PreToolUse is refused |
| `color`, `cacheTtl`, observer keys | UI, cache TTL, observer pairing |

Collision precedence (later wins): built-in < plugin < user settings < project settings < flag settings < policy settings. Plugins MAY register agents at runtime.

### 2.6 Workflow journal entry

```
JournalEntry { key: str, kind: "started" | "result" | "failed", value?: any }
key_i = H(prompt_i, opts_i, key_{i-1})       # chained, so a key encodes the whole prefix
```

### 2.7 Cron job

```
CronJob { id, cron: "m h dom mon dow" (local time), prompt, recurring=true,
          durable=false, kind?: "loop", createdAt }
```
Durable jobs persist in `.claude/scheduled_tasks.json`; others are session-only.

---

## 3. Behavioural requirements

### Agent tool & launch
- **R1** Agents MUST run in the background unless the caller explicitly passes `run_in_background: false` (or a definition/role forces async). *Observed.*
- **R2** Launch checks MUST run in the order of §4.1 and each failure MUST produce a model-readable error saying what to do instead (e.g., do the task directly; do not retry). *Observed.*
- **R3** Nesting depth MUST be capped (default 3; env `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`); at the cap the Agent tool MUST also be removed from the child's tool pool. *Observed.*
- **R4** Concurrent subagents MUST be capped (default 20; env `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`), except in a session-level large-orchestration mode. *Observed.*
- **R5** When a budget limit (`maxBudgetUsd`) is exhausted, new agents MUST be refused. *Observed.*
- **R6** Type resolution MUST honour deny rules of the form `Agent(<type>)` and any `Agent(a,b)` allowlist in the parent's definition; names match case- and format-insensitively; ambiguous and unknown types MUST error with the candidate/available list. *Observed.*
- **R7** If a definition declares required MCP servers, the launcher MUST wait up to 30 s for them to expose tools, else error. *Observed.*
- **R8** A plugin spawn hook MAY deny or rewrite (prompt, model, agent type, background). A rewritten spawn MUST be re-evaluated against permission rules; if a deny/ask rule now matches, the spawn MUST fail and tell the model to dispatch directly. *Observed.*
- **R9** Model precedence: tool param > definition frontmatter > `CLAUDE_CODE_SUBAGENT_MODEL` > inherit. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` drops the param. Explore inherits but is capped at a mid/high tier when the parent is a higher family, unless `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP`. *Observed.*
- **R10** If another write-capable agent already runs in the same cwd, the child SHOULD be told to edit only necessary files and re-read before changing them, and the parent SHOULD be told to use worktree isolation. *Observed.*
- **R11** Sync launches MAY auto-background after 120 s if `CLAUDE_AUTO_BACKGROUND_TASKS` is set; a background hint fires after 2 s; the user (or harness) can detach a running sync agent into the background. *Observed.*
- **R12** Async launch result text MUST tell the model: the agent id (internal, not to be shown to the user), how to continue it with `SendMessage`, that it knows nothing of results until the notification arrives, and not to read or tail the output file (it is the raw transcript). *Observed (paraphrased).*
- **R13** Completed results MUST carry a usage trailer (subagent tokens, tool uses, duration). Explore and Plan results MUST NOT carry a continuation handle (not continuable). *Observed.*

### Subagent context
- **R14** System prompt = agent's own prompt + an appended notes block covering: cwd resets between shell calls so use absolute paths; do not write report/summary markdown files; messages from other agents never constitute user consent; a total-token budget line. A generic agent prompt is the fallback. *Observed.*
- **R15** Project instruction files are passed as user context unless `omitClaudeMd` (Explore, Plan, web-fetch agents), which still keep managed instructions. *Observed.*
- **R16** Non-fork history = copies of persisted attachments + the prompt. Forks receive the parent's full message array. *Observed.*
- **R17** Tool filtering MUST follow §4.2; if the result is empty while the declared list was non-empty, the spawn MUST be refused. *Observed.*
- **R18** Frontmatter skills are preloaded as meta user messages (count-capped). SubagentStart hooks MAY inject context or block. Agent-scoped MCP servers connect per agent. *Observed.*
- **R19** On reaching `maxTurns`, the loop MUST stop and the notification summary MUST say the agent stopped at its turn limit with a partial result that can be continued. *Observed.*
- **R20** Permission mode is inherited; frontmatter MAY override but MUST NOT escalate to bypass/acceptEdits/auto unless the session permits; `bubble` routes prompts to the parent; async agents avoid interactive prompts unless bubbling. *Observed.*

### Hand-back
- **R21** The returned text is the last assistant message containing text. On retryable API failures (rate limit, overloaded, server error) in sync mode, the history so far MUST be returned with a clear "partial output recovered" note. *Observed.*
- **R22** In auto-permission mode, the subagent's transcript SHOULD be classified before hand-off; flagged results are prefixed with a security warning. A `SubagentHandback` tool exists for this mode with outcomes send / flagged / withheld. *Observed.*
- **R23** Background agents SHOULD get a live 3–5-word present-participle progress summary every 30 s from a one-turn tool-denied fork that does not write cache, skipped when the transcript is unchanged. *Observed.*
- **R24** Stall watchdog: an async agent with no stream progress for `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` (default stream-watchdog + 300 s, or 600 s without a watchdog; ≤ 2^31−1) MUST be aborted, but firing MUST be deferred while tool calls are in flight. *Observed.*

### Built-in agents
- **R25** Provide: `general-purpose` (all tools; instructed to finish fully without gold-plating and not to re-delegate the whole task); `statusline-setup`; `Explore` and `Plan` (read-only: no Agent, Edit, Write, NotebookEdit, plan-exit tools; inherit model; omit project instructions; Explore emphasises parallel search, Plan ends with a short critical-files list); a web-fetch agent (15 turns, WebFetch only, treats fetched content as untrusted); `claude-code-guide` (small model, `dontAsk`, read/search/web tools, prompt enriched with the user's configured skills/agents/MCP/settings; excluded for SDK entrypoints). Coordinator mode replaces the list with worker agents. Workflow agents use a definition that disallows user messaging, Agent and Workflow (workflow agents cannot spawn agents). *Observed.*

### Forks
- **R26** A fork (`subagent_type: "fork"`) uses all tools, 200 max turns, inherited model, `bubble` mode, and an empty own prompt; it MUST reuse the parent's rendered system prompt and byte-identical tools and messages (§4.3). Forking is gated by `CLAUDE_CODE_FORK_SUBAGENT`; when on, all agents run async. *Observed.*
- **R27** The fork directive MUST tell the worker: the inherited transcript is reference, not its situation; do not spawn subagents; report once and stop; begin by restating its task in one line. In a worktree it MUST be told to translate parent paths to its worktree root. Recursive and remote forks MUST be refused. *Observed (paraphrased).*

### Continuation & stop
- **R28** `SendMessage({to: agentId | name | "main", message, summary})` to a finished agent MUST resume it from its persisted transcript, re-resolving its definition and refusing if no longer trusted/allowed. *Observed.*
- **R29** `TaskStop` (legacy aliases `KillShell`, `KillBash`) accepts a task id, teammate name or agent name; stopped workers can be continued later. *Observed.*
- **R30** Coordinator mode (`CLAUDE_CODE_COORDINATOR_MODE=1`) swaps in an orchestration prompt whose required content is: role; tools (Agent, SendMessage, TaskStop); worker capabilities; phased flow research → synthesis → implementation → verification; always synthesize findings yourself; when to continue vs spawn; and that user-approved actions must be executed by a fresh worker quoting the user's exact approval. The coordinator's own shell is restricted to verifiably read-only commands. *Observed.*

### Teams (experimental)
- **R31** Enabled by `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` or `--agent-teams`. Named spawns create teammates in-process or in terminal-multiplexer panes (the latter launches a new CLI process with env passthrough and flags for permission mode, model, effort, settings, plugin dir, MCP config). The roster is flat: teammates cannot spawn teammates, and in-process teammates cannot spawn background agents. *Observed.*
- **R32** Mailbox per member: a JSON array file under the team directory, written with a lockfile; schema-invalid entries pruned. Protocol messages: shutdown request/response, plan approval request/response (only the lead approves). `team_name` is ignored: one implicit team per session. *Observed.*
- **R33** Cross-session peers: `ListAgents` lists local, remote-control and cloud sessions; incoming peer messages are wrapped in a cross-session envelope; a one-shot idle notification is available; the prompt MUST warn against laundering permissions across sessions. *Observed.*

### Workflow tool
- **R34** Opt-in only: the model may call it only if the user explicitly asked for a workflow (or the large-orchestration mode keyword / session mode), a skill instructs it, or a named workflow was requested. *Observed.*
- **R35** Input: one of `script | name | scriptPath`, plus `args`, `resumeFromRunId` (pattern `wf_` + ≥6 of `[a-z0-9-]`). Every script is persisted under the session directory for editing and re-run. *Observed.*
- **R36** Scripts MUST begin with a pure-literal `export const meta = {name, description, phases?}`. The runtime exposes `agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, and `workflow()` (one nesting level). Wall-clock and randomness sources MUST throw (they break deterministic resume). *Observed.*
- **R37** `agent(prompt, opts)` options: `label`, `phase`, `schema`, `model`, `effort`, `isolation: 'worktree'`, `agentType`, `disallowedTools`, `bashCommandClamp` (each entry must be a `Bash(...)` pattern), `stallMs`. Malformed narrowing options MUST refuse the spawn rather than run un-narrowed. *Observed.*
- **R38** With `schema`, the agent MUST be given a `StructuredOutput` tool and instructed to call it exactly once; without, its final text is returned verbatim as the value. *Observed.*
- **R39** Caps: concurrency `min(16, max(2, cpus−2))`; 1000 `agent()` calls per run; a hard token budget from a user directive (e.g. "+500k"); a max item count per `parallel`/`pipeline`. `parallel` and `pipeline` resolve failed items to `null` and never reject. *Observed.*
- **R40** Resume MUST return journaled results until the first key miss and then run every later call live (longest unchanged prefix). *Observed.*
- **R41** On usage-limit errors, agents wait for reset and re-run, at most 2 waits per run, with 30–90 s jitter. *Observed.*
- **R42** Agents sharing a prompt prefix SHOULD be staggered: one leader per prefix key; followers wait up to `CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS` (default 5000) for the leader's first response; the key stays warm 270 s. *Observed; purpose inferred.*

### Background shells, MCP, detach
- **R43** Shell `run_in_background` returns an id and an output file path the model may read for interim output; variants exist for manual backgrounding, timeout-to-background, and backgrounding so a newly arrived user message can be delivered. The timeout becomes the background lifetime (default 600,000 ms). *Observed.*
- **R44** MCP tool calls running past 120 s (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`) MUST become background tasks that notify on completion. *Observed.*
- **R45** A foreground tool call MAY be detached when a user message arrives, except when PostToolUse hooks are configured; a ceiling applies. *Observed.*

### Monitor
- **R46** Input `command` (each stdout line is an event) or `ws` (each text frame is an event), plus `description` included in every notification. `timeout_ms` default 300,000, max 3,600,000; in bounded mode capped at 30 min (10 min on remote/desktop) with a single expiry notice so the model can re-arm. *Observed.*
- **R47** Batching and flow control per §4.6. WebSocket targets MUST be SSRF-checked (no private, link-local or metadata ranges) and require permission each time. *Observed.*

### Cron
- **R48** Standard 5-field cron in local time; at most 50 jobs; `recurring` default true; `durable` writes project file; teammates cannot create durable jobs; auto mode requires classifier review. *Observed.*
- **R49** Scheduler per §4.7: 1 s tick, fire only when idle, single-owner lock for durable jobs, file watching, missed one-shots surfaced as "ask the user first" rather than executed, recurring jobs expire after 7 days with one final fire, deterministic jitter, cache-lead. *Observed.*
- **R50** The CLI SHOULD add a marked block to `.git/info/exclude` covering its runtime files (scheduled tasks, worktrees, mailbox…). *Observed.*

### ScheduleWakeup and /loop
- **R51** `/loop [interval] <prompt>` (alias `proactive`) parses a leading `\d+[smhd]`, a trailing "every N unit", or none (dynamic mode). Fixed intervals use CronCreate and run immediately; intervals ≥60 min or daily phrasing first offer a cloud scheduled routine via a user question. *Observed.*
- **R52** `ScheduleWakeup { delaySeconds, reason, prompt, noop?, stop? }` (prompt = the loop text or a dynamic-loop sentinel): delay clamped to [60, 3600]; target rounded up to the minute; delays ≤300 s pulled back to stay within a 285 s window; stored as a session cron entry `kind: "loop"`. If a tick ends without rescheduling, arm one 1200 s keepalive; after that the loop ends as model-stopped. Loops expire after 7 days. *Observed.*
- **R53** The ScheduleWakeup guidance MUST adapt to the cache TTL: under a 5-minute TTL advise not choosing ~300 s (either stay ≤270 s or go ≥1200 s); under a 1-hour TTL advise not reasoning in cache windows. When an external event gates progress, arm a Monitor as the primary wake and a 1200–1800 s wakeup as heartbeat. *Observed.*

### Worktrees
- **R54** `EnterWorktree` only when the user (or project instructions/memory) explicitly mentions worktrees. Creates `.claude/worktrees/<name>` on a new branch; base from setting `worktree.baseRef` (`fresh` = remote default branch, default; or `head`); falls back to WorktreeCreate/WorktreeRemove hooks outside git. `path` enters an existing worktree; outside the managed dir this needs approval (it moves the permission root). Name segments `[A-Za-z0-9._-]`, ≤64 chars; existing name is resumed, or reset if fully merged. Refused from a subagent with a pinned cwd. *Observed.*
- **R55** `ExitWorktree { action: keep | remove, discard_changes? }`: `remove` refuses on uncommitted files or commits ahead of the original HEAD unless `discard_changes`; never removes a worktree entered via `path`; no-op outside an EnterWorktree session. *Observed.*
- **R56** Agent worktrees (name `agent-<id>`) are removed after the run only if clean and HEAD unchanged; otherwise kept and reported. *Observed.*
- **R57** A desktop host MAY maintain a persistent worktree pool with leases and rebinding of donor worktrees, copy git-ignored config (`.claude/settings*.json`, `.mcp.json`, `CLAUDE.md`, `CLAUDE.local.md`) plus a `.worktreeinclude` overlay, skip heavy directories and runtime files, and support remote worktrees over SSH. *Observed.*

---

## 4. Algorithms

### 4.1 Launch pipeline

```python
def launch(ctx, inp):
    if depth(ctx) >= max_depth():                 raise Refuse("nesting limit; do it yourself")
    if ctx.is_teammate and inp.name:              raise Refuse("roster is flat")
    agent = resolve_type(inp.subagent_type or "general-purpose",
                         deny=deny_rules("Agent"), allow=ctx.def.allowed_agent_types)
    if budget_exhausted():                        raise Refuse("budget reached")
    if running_subagents() >= max_concurrent() and not large_orchestration_mode():
        raise Refuse("concurrency limit; do not retry")
    run_async = inp.isolation == "remote" or (
        (inp.run_in_background is True or agent.background or default_async(ctx, inp))
        and not background_disabled())
    # default_async: coordinator(non-teammate) or forced-async or
    #               (caller not teammate/headless-subagent and run_in_background is not False)
    if agent.required_mcp: wait_for_mcp_tools(agent.required_mcp, timeout=30)
    if (hook := plugin_spawn_hook(inp)):
        if hook.deny: raise Refuse(hook.reason)
        if hook.rewritten and permission_verdict(hook.rewritten) != permission_verdict(inp):
            raise Refuse("dispatch it directly")
        inp = hook.rewritten or inp
    model = inp.model or agent.model or env("CLAUDE_CODE_SUBAGENT_MODEL") or ctx.model
    wt = create_worktree(f"agent-{new_id()}") if (inp.isolation or agent.isolation) == "worktree" else None
    if other_writer_in_cwd(ctx.cwd) and not wt: warn_child_and_parent()
    child = build_child(ctx, agent, inp, model, wt, is_async=run_async)   # §4.2
    if run_async:
        tid = registry.register(child); spawn(child.run, on_done=lambda r: notify(tid, r))
        return AsyncLaunched(agentId=tid, outputFile=child.transcript_path)
    return run_sync_with_detach(child, auto_bg_ms=120_000 if env("CLAUDE_AUTO_BACKGROUND_TASKS") else 0,
                                hint_after_ms=2_000)
```

### 4.2 Tool pool filtering for a subagent

```python
NEVER_FOR_SUBAGENTS = {plan_enter, ask_user_question, vcs_connect, browser_setup_offer,
                       mcp_wait, mcp_refresh, "ScheduleWakeup", notifications_read,
                       goal_propose, plugin_publish, end_conversation, skill_propose,
                       "Workflow", account_memory_tools}
ASYNC_ALLOW = {"Read","Grep","Glob","Bash","PowerShell","Edit","Write","NotebookEdit",
               "WebFetch","WebSearch","TodoWrite","Skill","ToolSearch","StructuredOutput",
               "EnterWorktree","ExitWorktree","LSP","Monitor","TaskStop","SendMessage",
               "Artifact", plugin_search_tools}                # + every mcp__* tool
TEAMMATE_EXTRA = {"TaskCreate","TaskGet","TaskList","TaskUpdate","CronCreate","CronDelete","CronList"}

def filter_tools(pool, agent, is_async, is_teammate, depth):
    out = [t for t in pool if t.name not in NEVER_FOR_SUBAGENTS]
    if agent.permission_mode != "plan": out = [t for t in out if t.name != "ExitPlanMode"]
    if depth >= max_depth():            out = [t for t in out if t.name != "Agent"]
    if is_async:
        allow = ASYNC_ALLOW | (TEAMMATE_EXTRA if is_teammate else set())
        out = [t for t in out if t.name in allow or t.name.startswith("mcp__")]
    if agent.tools not in (None, ["*"]):
        out = [t for t in out if matches_any(t, agent.tools)]   # supports mcp__srv / mcp__srv__*
    out = [t for t in out if not matches_any(t, agent.disallowedTools or [])]
    if not out and agent.tools: raise Refuse("zero tools after filtering")
    return out
```

### 4.3 Cache-identical fork construction

```python
def build_fork(parent, directive, worktree=None):
    if parent.is_fork:           raise Refuse("no recursive forks")
    if parent.isolation_remote:  raise Refuse("remote cannot inherit context")
    last = clone(parent.messages[-1])                 # last assistant message
    placeholders = [tool_result(u.id, "fork started; running in background")
                    for u in last.tool_uses]           # answer EVERY pending call
    text = fork_directive(directive) + (path_translation_note(worktree) if worktree else "")
    return Child(system=parent.rendered_system_prompt,
                 tools=parent.exact_tools,              # byte-identical
                 messages=parent.messages[:-1] + [last, user(placeholders + [text])],
                 max_turns=200, permission_mode="bubble", model=parent.model)
```

### 4.4 Notification queue

```python
def notify(task_id, result):
    queue.push(Message(role="user", mode="task-notification", priority="next",
                       content=reminder(render_task_notification(task_id, result))))

def at_tool_round_or_idle(session):        # drained between tool rounds and when idle
    while queue: session.append(queue.pop())
    if session.idle and queue_was_nonempty: session.start_turn()
```

### 4.5 Workflow runtime with journal

```python
class Run:
    def __init__(self, run_id, journal):
        self.journal, self.prev_key, self.live, self.calls = journal, ROOT, False, 0
    async def agent(self, prompt, opts):
        validate_narrowing(opts)                       # refuse on malformed clamps
        self.calls += 1
        if self.calls > 1000: raise AgentCapError
        key = H(prompt, canonical(opts), self.prev_key); self.prev_key = key
        if not self.live:
            hit = self.journal.get(key)
            if hit and hit.kind == "result": return hit.value
            self.live = True                           # first miss → everything after runs live
        self.journal.append(key, "started")
        async with self.sem:                           # min(16, max(2, cpus-2))
            await prefix_stagger(prompt_prefix_key(prompt, opts))
            try:
                v = await run_child(prompt, opts, retries_on_usage_limit=2)
                self.journal.append(key, "result", v); return v
            except Exception as e:
                self.journal.append(key, "failed", str(e)); raise
    async def parallel(self, thunks):
        return [r if ok else None for ok, r in await gather_settled(thunks)]
    async def pipeline(self, items, *stages):         # each item flows through stages independently
        return await self.parallel([chain(item, stages) for item in items])

async def prefix_stagger(k):
    if warm(k): return                                 # warm for 270 s after first response
    if claim_leader(k): return
    await wait_first_response(k, timeout_ms=env_int("CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS", 5000))
```

### 4.6 Monitor flow control

```python
BATCH_MS, LINE_CAP, BATCH_CAP, BUF = 200, 500, 3000, 1 * MiB
bucket = TokenBucket(capacity=10, refill_every_ms=2000)
def on_line(line):
    pending.append(line[:LINE_CAP]); schedule_flush(after=BATCH_MS)
def flush():
    text = "\n".join(pending)[:BATCH_CAP]; pending.clear()
    if bucket.take(): notify(monitor_id, text, suppressed=suppressed_count); suppressed_count = 0
    else:
        suppressed_count += 1
        if suppressed_for() >= 30_000: kill(monitor, reason="too much output; use a narrower source")
```

### 4.7 Cron scheduler and jitter

```python
def tick():                              # every 1 s
    if not repl_idle(): return
    for job in jobs():
        if job.durable and not hold_project_lock(): continue   # pid+start-time lock; retry 5 s
        if due(job, now() + lead(job)):
            if job.recurring and age(job) > 7 * DAY: fire(job); delete(job); continue
            fire(job)

def frac(job_id): return int(hex_digits(job_id)[:8], 16) / 2**32        # stable in [0,1)

def lead(job):
    if is_every_n_minutes(job) and 300 <= period_s(job) < 315: return 15     # keep 5-min cache warm
    if not job.recurring and job.minute in (0, 30): return frac(job.id) * 90 # spread fleet load
    return 0

def recurring_delay(job, frac_cfg=0.5, cap_ms=30*MIN):   # actual live config may be smaller
    return min(frac(job.id) * frac_cfg * period_ms(job), cap_ms)

def on_startup_missed_oneshot(job): queue_reminder("ask the user before running", job)
```

### 4.8 Wakeup target

```python
def wakeup_target(delay):
    delay = clamp(delay, 60, 3600)
    t = ceil_to_minute(now() + delay)
    if delay <= 300 and t - now() > 285: t -= 60      # stay inside TTL minus 15 s lead
    return t
```

### 4.9 Agent worktree cleanup

```python
def after_agent(wt):
    if git_clean(wt) and head(wt) == wt.start_head: remove(wt); clear_meta(wt); return None
    return (wt.path, wt.branch)                        # reported to parent
```

---

## 5. Constants & knobs

| Name | Value |
|---|---|
| Agent result cap | 100,000 chars |
| Max spawn depth | 3 (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`) |
| Max concurrent subagents | 20 (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`) |
| Required-MCP wait | 30 s |
| Background hint | 2 s |
| Sync auto-background | 120 s (`CLAUDE_AUTO_BACKGROUND_TASKS`) |
| Progress summary interval | 30 s |
| Stall timeout | watchdog + 300 s, or 600 s (`CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS`) |
| Fork max turns | 200 |
| Web-fetch agent max turns | 15 |
| Workflow concurrency | min(16, max(2, cpus−2)) |
| Workflow agent calls | 1000 |
| Usage-limit waits | 2 per run, jitter 30–90 s |
| Prefix stagger | 5000 ms; warm 270 s (`CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS`) |
| Run id pattern | `wf_` + `[a-z0-9-]{6,}` |
| Background shell lifetime | 600,000 ms default |
| MCP auto-background | 120 s (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`) |
| Monitor timeout | 300,000 default, 3,600,000 max; bounded 30 min / 10 min |
| Monitor batching | 200 ms; 500 chars/line; 3000/batch; 1 MiB buffer |
| Monitor rate | bucket 10, +1 / 2 s; kill after 30 s suppression |
| Cron | ≤50 jobs; 1 s tick; 5 s lock retry; 300 ms write-stability; 7-day expiry |
| Cron jitter | recurring frac 0.5 cap 30 min (code default); one-shot ≤90 s early on :00/:30; 15 s cache lead |
| Wakeup | [60, 3600] s; 285 s window; keepalive 1200 s ×1; heartbeat 1200–1800 s |
| Worktree names | `[A-Za-z0-9._-]`, ≤64 chars |
| Other env | `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`, `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP`, `CLAUDE_CODE_FORK_SUBAGENT`, `CLAUDE_CODE_COORDINATOR_MODE`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` |
| Settings | `worktree.baseRef`, `maxBudgetUsd` |

---

## 6. Rationale

- **Async by default** keeps the parent responsive; it requires explicit anti-fabrication guidance because models otherwise invent results while waiting.
- **Continuation handles** make follow-ups cheap: resuming from transcript preserves the child's full context instead of re-briefing.
- **Forks** exploit prompt caching: the inherited prefix is nearly free, and placeholder tool results keep the message sequence valid.
- **Role-based filtering** prevents subagents from interacting with the user, changing session state, or scheduling, while permitting role-specific extras.
- **Re-checking rewritten spawns** closes an easy permission-laundering path through extensions.
- **Deterministic workflow scripts** move fan-out control out of the model; chained journal keys give exact "longest unchanged prefix" resume; banning time/randomness keeps replays valid.
- **Unified notifications** give one delivery path with flow control at the source, protecting the context window.
- **Cache-aware timers** avoid paying full prefix cost just after expiry; jitter spreads fleet load off popular minute marks.
- **Conservative worktree cleanup** never destroys unmerged work.

---

## 7. Harness design takeaways

1. **T1 Default to async delegation and design against racing.** Tell the model it knows nothing until the notification arrives and should report status, not guess; include an example of a user asking mid-wait.
2. **T2 Return a continuation handle.** Every result carries an id and how to continue it; continuation resumes from a persisted per-agent transcript; decide per type whether continuation is allowed.
3. **T3 Use cache-identical forks for context offload.** Exact parent prompt, tools and messages plus placeholder tool results and a short directive; fork when the intermediate output won't be needed again; keep directive style (fork) distinct from briefing style (fresh agent).
4. **T4 Layer hard caps with model-readable errors.** Depth, concurrency, budget, workflow call cap and zero-tool refusal, each telling the model what to do instead.
5. **T5 Filter subagent tools by role as well as definition.** A global never-for-subagents set, an async allowlist, teammate extras, then frontmatter allow/deny; unresolvable narrowing is a refusal, never a silent widening.
6. **T6 Route extension rewrites back through permissions.** Re-check rewritten spawns and refuse on a changed verdict; user approvals are quoted verbatim into a fresh worker, never relayed.
7. **T7 Move orchestration into deterministic code at scale.** Script with agent/pipeline/parallel, structured output via a forced tool, banned nondeterminism, a prefix-keyed journal; prefer pipelines over barriers; failures resolve to null.
8. **T8 Make structured output a tool call.** A schema-bearing agent must call a dedicated tool exactly once, so validation errors return as tool errors and the model retries.
9. **T9 Treat the prompt cache as a scheduling input.** Early-fire 5-minute crons, snap wakeups under the TTL, advise against ~300 s waits, stagger same-prefix workflow agents, and skip cache writes for throwaway forks.
10. **T10 Unify async sources behind one notification queue.** Agents, shells, monitors, MCP auto-background, cron and wakeups all enqueue the same notification; add source-side flow control (batching, token bucket, auto-kill).
11. **T11 Isolate parallel writers and clean up conservatively.** Warn on shared-cwd writers, offer worktree isolation, remove only provably clean worktrees you created, and give absolute-path guidance.
12. **T12 Add watchdogs and partial results.** Stall timeouts deferred during tool calls, and recovered partial output on retryable API errors.

---

## 8. Uncertainties

- The per-call item cap for `parallel`/`pipeline` and the skill-preload count cap were not resolved.
- The team directory base path is inferred (probably under the user config dir); the in-process teammate runner was not examined.
- The `SubagentHandback` tool prompt and the hand-off classifier prompt were not extracted.
- A second-generation workflow engine (row journal with steering verbs) appears dormant; whether it ships elsewhere is unknown.
- Remote-isolation agents: only gating and launch were traced, not the permission relay.
- Cron jitter: code defaults (50%, 30 min cap) differ from user-facing guidance (≈10%, 15 min cap); live values are server-configured.
- How desktop hosts surface background tasks and teammates was not examined beyond worktree management.
