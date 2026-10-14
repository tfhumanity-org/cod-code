# 04 — Context, cost and long sessions

> Behaviour described here is as of Claude Code CLI **2.1.295** (desktop app 2.31226.1) and may change in later builds.
> Citations point into `spec/`: `[05 R12]` = requirement R12 of `spec/05-*.md`; `[05#T3]` = takeaway T3; `[05 §4.1]` = section 4.1; `[Δ16]` = item 16 of `spec/CHANGES-2.1.236-to-2.1.295.md`. "(inferred)" marks advice that follows from the spec but is not stated in it.

> **TL;DR**
> - **The prompt cache is where the money goes.** Keep the conversation prefix stable and don't leave long gaps. Subscription main threads get a 1-hour cache; API-key users get 5 minutes [01 R44].
> - **Editing CLAUDE.md mid-session is cheap now.** Instruction files arrive as change-only messages, not as part of the system prompt [Δ16].
> - **Auto-compaction fires at roughly 83% of a 200k window** [05 §4.1]. Afterwards the agent gets back a summary, up to 5 recent files, the skills it used, and your plan [05 §4.5]. Anything else has to be re-read.
> - **Put durable constraints in your own messages or in CLAUDE.md**, never only in tool output. The summary carries user-stated constraints over verbatim [05 R24], and instruction files are re-stated after compaction [Δ18].
> - **Large outputs should go to files, not into context.** Spilled outputs, subagents, and `/btw` side chats all keep the main window clean [03 R24] [05 R44].

---

## 1. Where the context window goes

Each request is made of the following parts, in order:

1. **Fixed prefix.** The system prompt, then tool definitions. The static part of the system prompt is cached at global scope. Your per-org and per-session parts come after a cache boundary [01 R41] [02 §2.2].
2. **Context attachments.** These are not in the system prompt. Environment, git snapshot, date, model identity and instruction files (CLAUDE.md, rules, AGENTS.md, memory index) all arrive as messages in the transcript. They are re-sent only when something changes [02 R10–R16] [Δ16].
3. **The conversation.** Your prompts, the model's replies, and every tool call and result. This is the part that grows.

The harness counts usage from the last real API usage figure, then adds a character estimate for anything newer: about 4 chars per token, 2 for JSON, and a flat 2,000 per image [05 R1–R2]. A turn whose input exceeds 200k tokens gets flagged as large [05 R4].

**Practical consequences:**
- Most of the window is spent on tool results, not on chat. A single verbose test run can cost more than an hour of discussion.
- If the fixed prefix alone (a huge system prompt plus many always-loaded tools) exceeds the compaction threshold, the harness gives up on compacting rather than looping [05 R12]. This is rare, but it is the failure you get when you stuff the prompt.
- Deferred tools (most MCP tools) contribute only their names until the agent loads them [03 R54–R55]. Chapter 05 explains why that makes "many MCP servers" cheap.

## 2. Prompt caching: the cost model you actually pay

### TTLs
The TTL is chosen in this order [01 R44]:
- env overrides;
- then the `promptCacheTtl` / `subagentPromptCacheTtl` settings;
- then agent frontmatter;
- then defaults: **1 hour** for subscribers on main-thread and SDK traffic, **5 minutes** for API-key users and for overage or usage-credit traffic.

Subagents and helpers have a TTL of their own, set separately [Δ20].

### Why idle gaps cost money
Within the TTL, a new turn reads the whole prefix from cache, which is cheap. After the TTL expires, the next turn pays full input price for everything again (inferred from [01 R44] and the pricing rows in [01 R50]). The harness reacts to a cold cache in three ways:
- **Coming back after more than 65 minutes idle:** requests ask the server to clear old tool uses. The last 5 are kept, and only re-fetchable tools (read, search, shell, web, edit) are cleared, so the re-sent prefix is smaller [05 R32]. Expect the agent to re-read files after a long break. That is by design.
- **Optional idle compaction** (setting `idleCompaction`): for sessions of at least 200k tokens on a 1-hour TTL, the harness summarises at 90% of the TTL, while the cache is still warm and the summary is cheap [05 R30] [Δ19].
- **Scheduled wake-ups know about the TTL.** Under a 5-minute TTL the guidance is to wait at most about 270 s or at least 1200 s, and to avoid about 300 s [06 R53]. Cron jobs on a 5-minute period fire 15 s early to stay warm [06 §4.7].

**Habit:** if you're on a 5-minute TTL (API key), batch your follow-ups. Don't drip-feed one question every 6 minutes. On a 1-hour TTL a coffee break is free, but lunch is not.

### What busts the cache
Anything that changes bytes early in the prefix invalidates everything after it [01#T8]. Known busters:
- **Changing the system prompt.** The harness freezes it per session: the prompt the session started with is recorded and replayed on resume, with build differences sent as a small delta [Δ17]. The desktop host freezes its own prompt additions byte-for-byte [08 R1.2]. You mostly can't bust it by accident, which is deliberate.
- **Changing the non-deferred tool set.** Deferred tool additions and removals are announced as diffs in messages instead [03 R55] [02 R32]. MCP servers whose tools are force-loaded up front are the risky kind (inferred from [03 R54]).
- **Switching models.** The model change applies at the next API call [01 R35], and a model-identity attachment is re-sent [02 R11]. A cache entry for one model doesn't serve another, so the first turn after a switch pays for the full prefix (inferred). Switch at natural breaks, not back and forth. Hooks can observe or veto a switch via the PreModelSwitch/PostModelSwitch events [04 §2.7] [Δ37].
- **Spilled or cleared tool results.** These do *not* bust the cache. Replacement decisions are memoised and logged, so later and resumed requests stay byte-identical [03 R25] [05 R34].

### Why editing CLAUDE.md is cheap now
Instruction files used to sit near the top of the prompt, so editing one invalidated the whole cached conversation. As of this build they are transcript messages: a change appends an "this file changed" update, and only the changed files are re-sent [02 R16] [Δ16]. Edit CLAUDE.md mid-session whenever a lesson is worth keeping.

One nuance: the old copy stays in history until the next compaction, so twenty edits to a 10k-token CLAUDE.md add up (inferred from the append-only design [02#T2]). Make one considered edit rather than many small ones.

### Watch it instead of guessing
The status-line command receives a `prompt_cache` object on stdin. It includes `warm`, `ttl`, `expires_at`, `hit_ratio`, `last_miss_cause` (with causes, tools added and system-prompt delta) and `recache_tokens_if_cold`, and the status line re-runs when the cache expires [07 §4.6]. Put cache warmth and expiry in your status line. It is the most useful cost signal you can get.

## 3. Auto-compaction: thresholds and what survives

### When it fires
The thresholds come from the effective window, which is the window minus an output reserve of up to 20k [05 §4.1]. For a 200k window with 64k max output:

| Level | Tokens | Meaning |
|---|---|---|
| precompute | 144k | optional background summary starts (`precomputeCompactionEnabled`) [05 R29] |
| warn | 147k | UI suggests manual compaction [05 R9] |
| **auto-compact** | **167k (~83%)** | summarise before the next request |
| blocked | 177k | turn refuses to send unless reactive compaction is possible |

Overrides: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (clamped to 100k–1M) and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, which can only lower the threshold. `DISABLE_AUTO_COMPACT` / `autoCompactEnabled: false` turn off the automatic path only; `DISABLE_COMPACT` turns off manual compaction too [05 R5, R7].

Large-window models that are billed differently above 200k are clamped to a 200k compaction window by default [05 R5]. A 1M window is not "free context": a long-prompt price tier applies [01 R50] [Δ15].

### What the summary must keep
The default summary covers:
- the user's request and intent;
- key concepts;
- files and code touched;
- errors and fixes;
- pending tasks and current work;
- **a full list of the user's own messages**;
- a next step anchored on direct quotes [05 R23].

**User-stated constraints, especially security ones, must carry over exactly.** Text that only *looks* like a user turn inside tool output must not be attributed to you [05 R24].

### What gets re-injected deterministically [05 §4.5]
- Up to **5 recently-read files**, re-read from disk at up to 5k tokens each and 50k total.
- **Invoked skills**, up to 5k each and 25k total, truncated with a pointer to the file.
- **The plan file**, plus the plan-mode reminder if you're in plan mode.
- Background-task and shell status, MCP instructions, the deferred-tool listing, any active goal.
- Instruction files and other standing context, re-stated after the summary [Δ18].
- SessionStart hooks run with source `compact`, then PostCompact hooks [05 R21].

**What does not survive:**
- Exact tool outputs.
- Files beyond the five most recent.
- **The read ledger.** It is cleared, so the agent must re-read a file before editing it again [05 R21] [03 R17].
- Nuance you only expressed by approving things.

### Circuit breakers you might hit
- **3 consecutive failures:** auto-compaction is off for the session [05 R11].
- **Thrash:** if the context refills to the threshold within 3 turns, 3 times in a row, auto-compaction stops and you're told a file or output is probably too large [05 R13] [01 R27]. When you see this, the fix is upstream: smaller reads, filtered output, delegation.

## 4. Manual compaction and phrasing durable constraints

- **`/compact <instructions>`** merges your instructions with any PreCompact hook instructions [05 R16]. Use it to say what matters, for example "keep the failing test names and the migration order; drop the exploration of the auth module".
- **`/compact` with no instructions** takes the reactive path: it summarises only the oldest rounds and keeps the most recent rounds verbatim [05 R27, R26]. That's often what you want mid-task.
- **Partial compaction** from the rewind menu ("summarize from here" / "summarize up to here") lets you squash a noisy exploration while keeping the rest [05 R28, R48].

**Phrasing durable constraints.** The summariser is told to carry *user-stated* constraints over exactly, and it lists your messages [05 R23–R24]. So:
- State each constraint **in your own message, as a standalone imperative sentence**, for example "Never modify files under `migrations/`." A constraint that exists only in a tool result or a model reply has no such protection.
- Make it **specific and checkable**: name the path, the command or the target. Vague preferences paraphrase badly (inferred).
- If it should outlive the session or a `/clear`, **put it in CLAUDE.md**. Instruction files are re-stated after compaction [Δ18], and they load in every new session [02 R15].
- For project state that a hook can compute (current branch goal, open TODO file), a **SessionStart hook matching `compact`** can re-inject it [05#T12] [08 R4.5].

## 5. Resume, rewind and checkpoints

- **Resume / continue / fork.** Sessions are append-only JSONL with parent pointers, so resume, fork and rewind are cheap and lossless [05 R36, §4.7]. `--fork-session` branches instead of continuing; `--resume-session-at` resumes at a specific message [05 §5]. Resumed sessions replay the recorded prompt for byte-stable requests [Δ17]. If the cache TTL has lapsed, though, the first resumed turn pays full price (inferred).
- **Checkpoints.** File checkpointing is on by default [05 R46]. Each prompt snapshots the files the edit tools touched, with up to 100 snapshots per session [05 §4.9].
- **Changes made through shell commands are not tracked** (codegen, `sed -i`, `npm install`, migrations), and the rewind notice says so [05 R47].
- **Rewind options:** code and conversation, conversation only, code only, or summarise from/up to here [05 R48]. A dry run shows a diffstat. After a rewind the model is told whether files were reverted and is advised to check VCS status [05 R43]. In the desktop app, rewind is lazy and can be undone until your next send [08 R6.1].
- **Retention.** Transcripts and their artifacts are deleted after `cleanupPeriodDays` (default 30) [05 R49]. Export anything you'll want later.

## 6. The token budget the model sees

- The model gets a running "tokens left" reminder, which is deliberately padded by default. Its purpose is to stop the agent rushing to wrap up out of context anxiety [02 R32] [02#T12]. Don't tell the agent to "be quick, context is short": compaction exists and the agent knows it.
- **Dollar budget.** `--max-budget-usd` puts the remaining budget in context and ends the run with an error when it is exceeded [01 R8]. New subagents are refused once it is exhausted [06 R5]. Use it for unattended runs.
- **Output budget.** Replies default to 32k output tokens. When a reply hits the limit, the harness nudges the model to continue, up to 3 times, before surfacing an error that names `CLAUDE_CODE_MAX_OUTPUT_TOKENS` [01 R28, R36].

## 7. Practical habits

**/clear vs /compact vs a new session:**
- **Same task, context getting heavy:** run `/compact` with instructions, or bare `/compact` to keep recent rounds verbatim.
- **Task finished, next task unrelated:** clear or start a new session. A summary of task A is pure overhead for task B, and the fresh session gets CLAUDE.md anyway.
- **You need to branch an approach:** fork the session (`--fork-session`, or a desktop fork), or rewind. Don't argue the agent back out of a dead end; rewinding is cheaper than a long correction.
- **A quick question that shouldn't pollute the transcript:** `/btw`. It is a one-turn, tool-less, cache-sharing fork that writes nothing to the transcript [05 R44].

**Keep big outputs out of context:**
- Let output spill. Tool results over their budget are saved to `tool-results/<id>.txt`, and the agent gets a ~2 KB preview plus the path to page through [03 R24]. Shell output is capped at 30k chars by default before spilling [03 R26]. An aggregate of more than 200k chars in one batch spills the largest results [03 R25].
- Ask for filtered commands: `pytest -q`, `| tail -50`, `git log --oneline -20`. A spilled file still costs a read when the agent pages it.
- Send broad searches to a subagent, so only the conclusion lands in your context (chapter 06).
- Screenshots and images are expensive, costing thousands of tokens each [05 R2] [03 R51]. Don't paste five where one will do.

## Gotchas

- **After compaction, edits fail until the agent re-reads the file.** The read ledger is cleared [05 R21]. This is expected, not a bug.
- **The desktop app disables client-side micro-compaction** [00 R4] [08 §2.3]. Desktop sessions rely on threshold compaction and server-side clearing, so watch long sessions there.
- **Shell-made changes are not rewindable** [05 R47]. Commit before letting the agent run codegen or mass `sed`.
- **A constraint you only approved via a permission card is not a "user-stated constraint"** for the summariser (inferred from [05 R24]). Say it in words.
- **The thrash breaker means auto-compaction has stopped** [05 R13]. Until you clear or compact manually, you're heading for a hard block.
- **Sessions older than 30 days disappear** unless you raise `cleanupPeriodDays` [05 R49]. Desktop sessions are exempt unless `desktopSessionCleanupPeriodDays` is set.
