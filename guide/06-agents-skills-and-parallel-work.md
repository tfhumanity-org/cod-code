# 06 — Agents, skills and parallel work

> Behaviour described here is as of Claude Code CLI **2.1.295** (desktop app 2.31226.1) and may change in later builds.
> Citations point into `spec/`: `[06 R13]` = requirement R13 of `spec/06-*.md`; `[06#T3]` = takeaway T3; `[07 §4.1]` = section 4.1; `[Δ43]` = item 43 of `spec/CHANGES-2.1.236-to-2.1.295.md`. "(inferred)" marks advice that follows from the spec but is not stated in it.

> **TL;DR**
> - **Delegate to keep your context clean, not to look busy.** A subagent's 50 file reads cost you one summary. The subagent starts knowing only what you wrote in its brief [06 R16].
> - **Agents run in the background by default** and report through a notification [06 R1] [06 §4.4]. Continue a finished one with SendMessage, except **Explore and Plan, which can't be continued** [06 R13].
> - **Parallel writers need `isolation: worktree`.** Agent worktrees are cleaned up only if they are untouched [06 R10, R56].
> - **Skills trigger on their description plus `when_to_use`.** The listing has a budget of about 8,000 chars; over budget, low-priority skills degrade to name-only [07 §4.1] [02 R37].
> - **Desktop:** use the app's worktrees, PR monitor and auto-fix, side chats and rewind. Don't ask the agent to poll or babysit [08 §3.1, R10.x].

---

## 1. Subagents: when to delegate

**Delegate when:**
- The answer needs a **broad sweep** (many files, naming conventions, call sites) and you only want the conclusion. The built-in **Explore** agent exists for this. It is read-only and emphasises parallel search [06 R25].
- You want a **plan** without the planning noise in your thread. That is **Plan**, also read-only, and it ends with a short critical-files list [06 R25].
- The work is **independent and parallelisable**: several modules, several hypotheses, several PR reviews.
- The output is **big and disposable**, such as log triage or dependency audits. Results are capped at 100k chars anyway [06 §2.3].

**Do it inline when:**
- You already know the file and the symbol. A Grep and a Read is faster than briefing someone.
- The next step depends on the result **and** you need the full detail, not a summary.
- The change is small and touches things you've already read. A subagent has to re-read them.

### Brief like you're handing off to a contractor
A non-fork subagent's history is your prompt plus copies of persisted attachments [06 R16]. **It has not seen your conversation.** And **Explore and Plan don't get your CLAUDE.md** (`omitClaudeMd`), though managed instructions are kept [06 R15]. A good brief has:
- **The goal and how the result will be used**, e.g. "I'll use this to decide whether to split the module".
- **What you already know**, as absolute paths, symbols and the failing command. Subagents are told cwd resets between shell calls [06 R14].
- **Scope and breadth**: "medium" vs "very thorough", and which directories to search.
- **The shape of the answer**: list, table, file:line references, "conclusion first".
- **Constraints that matter**: "read-only", "don't touch `migrations/`". Messages from other agents never count as user consent [06 R14]. If an action needs your approval, you have to give it yourself.

### Background by default
- Agents run async unless the call passes `run_in_background: false` [06 R1].
- The launch result tells the parent it knows nothing until the notification arrives, and **not to read or tail the output file**, which is the raw transcript [06 R12].
- If you ask "what did it find?" mid-run, a well-behaved agent reports that the work is still running instead of guessing [06#T1].
- Background agents get a 3–5 word progress summary every 30 s [06 R23]. A stall watchdog aborts agents with no stream progress, but not while a tool is running [06 R24] [Δ45].
- Subagents now hand back through a dedicated tool. The report can be marked delivered, delivered with a warning, or withheld after a safety review [Δ43] [06 R22].

### Continuing agents
- **SendMessage** to a finished agent resumes it from its own transcript. The definition is re-checked [06 R28].
- This is far cheaper than re-briefing a fresh agent, because it keeps everything that agent learned [06 §6].
- **Explore and Plan results carry no continuation handle** [06 R13]. If you'll want follow-ups, use a general-purpose agent.
- **An agent that hits `maxTurns` is labelled truncated, and can be continued** [06 R19] [Δ44]. "Truncated" means "not finished", not "failed".
- **TaskStop** stops an agent or a shell. Stopped workers can be continued later [06 R29].

### Forks vs fresh agents
- A **fork** inherits your full conversation, rendered system prompt and tool list **byte-for-byte**, so its prefix is nearly free from cache [06 R26] [06 §4.3].
  - Forks always use your model and effort [Δ47], get 200 turns, and cannot fork again [06 R27].
  - They are opt-in via `CLAUDE_CODE_FORK_SUBAGENT`, and when that is on all agents run async [06 R26].
- **Use a fork** to offload work whose intermediate output you won't need again, but which needs the context you've built, e.g. "try approach B while I keep going with A" [06#T3].
- **Use a fresh agent** when the task is self-contained and your 150k-token context would only distract it.
- Fork directives are short ("here's your piece"); fresh-agent prompts are full briefings [06#T3]. Don't mix the styles.

### Caps you'll hit
- **Nesting depth: 3.** At the cap the Agent tool is removed (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`) [06 R3].
- **Concurrent subagents: 20** (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`). The error says not to retry [06 R4].
- **Budget.** Once `maxBudgetUsd` is exhausted, new agents are refused [06 R5].
- **Model.** Precedence is the tool param, then frontmatter, then `CLAUDE_CODE_SUBAGENT_MODEL`, then inherit. Explore is capped at a mid/high tier when you run a top-tier model [06 R9].
- **Async agents can't ask you questions.** Their tool pool excludes user-interaction tools, plan mode and scheduling [06 §4.2]. Put decisions in the brief.

## 2. Worktree isolation for parallel writers

- If another write-capable agent already runs in the same cwd, the new child is told to edit only what it must and to re-read files before changing them, and you're nudged toward worktree isolation [06 R10].
- **`isolation: "worktree"`** gives the agent its own checkout under `.claude/worktrees/` on a new branch [06 R54].
  - After the run it is **removed only if clean and HEAD unchanged**. Otherwise it is kept, and its path and branch are reported back [06 R56] [06 §4.9].
- The worktree base comes from `worktree.baseRef`:
  - `fresh` is the default and means **the remote default branch**;
  - `head` means your current HEAD [06 R54].
- **Gotcha:** with `fresh`, your unpushed commits and uncommitted changes are *not* in the agent's tree. Commit and push first, or switch to `head`.
- **`ExitWorktree remove` refuses** when there are uncommitted files or commits ahead, unless the agent is explicitly told to discard [06 R55].
- **The git stash stack is shared across worktrees** [02 R10]. Tell parallel agents never to stash, and to use WIP commits instead.
- Agent definitions and workflow agents can set `isolation: worktree` themselves [06 §2.5] [06 R37].

## 3. Writing skills that actually trigger

A skill is a `SKILL.md` in a directory. The directory name is the skill name, and nested directories are namespaced with `:` [07 R2]. Discovery looks in:
- `~/.claude/skills`;
- `.claude/skills` in every directory from cwd upward;
- plugins and `--add-dir` directories;
- subdirectories the agent touches later, which are picked up dynamically [07 R1, R4].

### How the model finds your skill
The model sees a **budgeted listing**: `name: description - when_to_use`.
- The budget is about 1% of the context window × 4 chars, ≈8,000 chars at 200k (`SLASH_COMMAND_TOOL_CHAR_BUDGET` or `skillListingBudgetFraction`).
- Each entry is capped at **1,536 chars** [07 §4.1] [02 R37].
- **Over budget, the lowest-priority skills degrade to name only.** They are not dropped, but a bare name rarely triggers anything (inferred). Bundled skills keep their descriptions.

**So:**
- **Lead the description with the trigger**, i.e. what the user says or what the task looks like. The `when_to_use` field is appended to it [07 §2.2].
- **Name nouns and verbs users actually type**: "deploy", "release notes", "flaky test". The skill is useless if the description reads like marketing copy.
- **Say when *not* to use it** if a neighbouring skill overlaps. Ambiguity costs a wrong invocation.
- **Prune skills you never use.** Every extra one pushes others toward name-only.
- **`paths:` globs** make a skill load only when matching files are touched [07 R5]. Good for framework-specific skills in a monorepo.
- When the user types `/name`, the model is told to invoke it only if you're asking to *run* it, not merely mentioning it [07 R11].

### Frontmatter that matters [07 §2.2]
- **`allowed-tools` / `disallowed-tools`**: temporary tool changes while the skill is active. They expire at the next user message [07 R8].
- **`model`, `effort`**: temporary overrides with the same lifetime.
- **`context: fork`** (+ `agent`, `background`): runs the skill as a subagent, so its work stays out of your context [07 R9]. Use it for heavy procedures (audits, migrations). Use inline for short checklists that need the conversation.
- **`disable-model-invocation`**: user-only. **`user-invocable: false`**: model-only.
- **`argument-hint`**, with **`$ARGUMENTS`** and **`${CLAUDE_SKILL_DIR}`** substituted in the body.

### Keep bodies short
- After compaction, invoked skills are re-injected at **up to 5k tokens each, 25k total**, and the agent is told not to re-run them [05 §4.5] [02 R32]. Put the procedure in `SKILL.md` and the reference material in sibling files the skill tells the agent to read.
- Files over 1 MB are skipped [07 R3].

## 4. Writing custom agents

Put agents in `.claude/agents/<name>.md` with frontmatter [06 §2.5]:

| Field | Use it for |
|---|---|
| `name`, `description` | required; the description is how the parent picks it |
| `tools` / `disallowedTools` | narrow the pool. `Agent(a,b)` limits which agent types it may spawn; `mcp__srv` matches a whole server |
| `model`, `effort` | `inherit`, alias or full id |
| `permissionMode` | can't escalate beyond what the session allows [06 R20] |
| `maxTurns` | budget; hitting it yields a continuable partial [06 R19] |
| `skills` | preload skills into the agent [06 R18] |
| `memory` | `user`/`project`/`local` memory; auto-adds Read/Write/Edit |
| `background`, `isolation` | always async; worktree or remote |
| `omitClaudeMd` | drop project instructions (keeps managed ones) |
| `mcpServers`, `hooks` | agent-scoped servers and hooks (a top-level PreToolUse is refused) |
| `initialPrompt`, `color`, `cacheTtl` | first prompt, UI colour, cache TTL |

**Notes:**
- When names collide, later sources win: built-in < plugin < user < project < flag < policy [06 §2.5].
- **A tool filter that matches nothing makes the spawn fail** rather than run with zero tools [06 R17]. Check spelling.
- Inside the sandbox, `.claude/agents` and `.claude/commands` are write-protected [04 R42]. Expect a prompt when the agent edits its own agent definitions.

## 5. Scheduling and loops

- **`/loop [interval] <prompt>`**: a leading `5m`, a trailing "every N unit", or no interval (self-paced) [06 R51].
  - Fixed intervals become session cron jobs and run immediately.
  - For intervals of 60 min or more, you're offered a cloud routine instead.
- **Self-paced wake-ups** are clamped to 60–3600 s and snapped to stay under the cache TTL [06 R52] [06 §4.8].
  - A loop that doesn't reschedule gets one 20-minute keep-alive and then ends.
  - Loops expire after 7 days [06 R52].
- **Cron**: 5-field, local time, ≤50 jobs. Jobs fire only when the session is idle. Recurring jobs expire after 7 days [06 R48–R49].
  - Durable jobs persist in `.claude/scheduled_tasks.json`.
  - A one-shot missed while closed is surfaced as "ask the user first", not run [06 R49].
- **Waiting on an external event?** Arm a Monitor as the primary wake and a long wake-up (20–30 min) as a heartbeat [06 R53].
- **Timers survive** compaction, resume and restart, and don't double-fire after a fork [Δ56].

## 6. Desktop app tips

- **Worktrees and branches.** Sessions can run in app-managed worktrees on `<prefix>/<slug>` branches. The prefix defaults to `claude` and is configurable [00 §5] [00 §4].
  - New trees get your `CLAUDE.md`, `.mcp.json`, `.claude/settings*.json`, plus anything listed in **`.worktreeinclude`** [08 R8.3]. Add your `.env.example`-style local config there.
  - In lazy-worktree mode, edits in your main checkout are blocked until the agent enters a worktree [08 R4.1, R8.1].
  - To pull in base-branch changes, the agent should use the app's base-sync tool rather than `git merge`/`pull`. It runs outside the sandbox [08 R8.4].
- **PR monitoring and CI auto-fix.** After a PR is open, the app polls it: every 60 s, or 5 min when the window is unfocused [08 §4.5].
  - It dedupes failures per head commit and check name, and waits up to 5 min for sibling checks [08 R10.2, R10.4].
  - It forwards review comments only from bots and owners, members or collaborators [08 R10.3].
  - **Turning auto-fix on is standing authorisation to fix and push** for the app's findings, but not for instructions quoted inside comments [08 R10.6].
  - The agent merges the base in and never rebases or force-pushes [08 §3.1]. It never enables auto-merge unless you ask.
- **Side chats and asides.**
  - A side chat is an ephemeral, read-only fork that doesn't touch the main transcript [08 R6.4].
  - Typing mid-turn gets an instant answer from an aside, which the main agent then treats as its own [08 R11.5].
- **Rewind.** It is lazy and undoable until your next send. File restore is separate and has a dry run. The agent is told exactly what happened to the files [08 R6.1–R6.3].
- **Spawned sessions.**
  - Suggestion chips (`spawn_task`) need a self-contained prompt [08 §2.6].
  - Child sessions report back as notes. The parent is woken once when all are idle, at most 10 times without a human message [08 R9.1–R9.3].
  - Never let two writers share files [08 §3.1].

## Gotchas

- **Explore and Plan don't see your CLAUDE.md** [06 R15]. Put critical project rules in the brief.
- **Explore and Plan can't be continued** [06 R13]. Use general-purpose if you expect follow-ups.
- **Don't read the agent's output file**. It's the raw transcript, so wait for the notification [06 R12].
- **`worktree.baseRef: fresh` ignores your local commits** [06 R54].
- **Dirty agent worktrees are kept, not deleted** [06 R56]. Clean them up periodically.
- **Skills degrade to name-only when you have too many** [07 §4.1], and a name-only entry rarely triggers.
- **Skill tool changes end at your next message** [07 R8]. They aren't a session-wide setting.
- **Subagent `REPORT.md`-style files are refused** [03 R34]. Ask for the report as the agent's reply.
- **Missed one-shot cron jobs don't run silently** after a restart; you're asked first [06 R49].
