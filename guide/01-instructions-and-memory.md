# 01 — Instructions and memory: what Claude actually sees

> Behaviour described here is as of CLI 2.1.295 and may change. Every claim cites the functional spec in `spec/` as `[file Rn]` (requirement), `[file#Tn]` (takeaway) or `[Δ n]` (item n in `CHANGES-2.1.236-to-2.1.295.md`). "(inferred)" means the spec itself marks the point as inferred, or it is my reading of a gap in the spec.

Most "Claude ignored my instructions" problems come from one of three causes. The file was never loaded. It was loaded but buried. Or it was a chat message that got summarised away. This chapter covers what gets loaded, in what order, and when. It also covers how to write instructions that still hold three compactions later, and how to use auto-memory without filling it with noise.

> **TL;DR**
> - Instruction files load Managed → User → Project/Local (filesystem root down to cwd) → auto-memory index. **Later means more weight**, so the CLAUDE.md nearest your cwd wins ties [02 R15].
> - In monorepos, use `.claude/rules/*.md` with `paths:` frontmatter. Those files stay out of context until a tool touches a matching path [02 R19]. Nested `CLAUDE.md` files arrive the turn **after** Claude first touches their subtree [02 R20].
> - There is no truncation. Oversized files only trigger a warning and still cost you tokens. Over 4 MiB, the file is silently skipped [02 R18].
> - `AGENTS.md` now loads by default in any directory that has no `CLAUDE.md` [02 R21a] [Δ 26]. If you keep both, import one from the other.
> - Auto-memory means one fact per file plus a short `MEMORY.md` index (200 lines / 25 KB cap). At most 5 memories are recalled per turn, under a session budget [02 R24, R28]. Save surprises, not code facts.

---

## 1. What gets loaded, and in what order

Instruction files are delivered as an `instructions` attachment in the message stream, never in the system prompt [02 R16]. They load in this order. Later entries sit later in context and carry more weight [02 R15]:

1. **Managed (organisation policy):** the policy-directory `CLAUDE.md`, a remotely managed string, the policy `claudeMd` setting, and managed `.claude/rules/`.
2. **User:** `~/.claude/CLAUDE.md` and `~/.claude/rules/**/*.md`.
3. **Project and Local:** for every directory from the filesystem root **down to your cwd**, in that order: `CLAUDE.md`, `.claude/CLAUDE.md`, any unconditional `.claude/rules/**/*.md`, and `CLAUDE.local.md`.
4. **`--add-dir` directories**, but only if `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is truthy.
5. **Auto-memory:** the `MEMORY.md` index.

Each file is labelled with its path and provenance: checked-in project, private project, auto-memory, organisation policy or private global [02 R16]. The model can tell which file said what and resolve conflicts with that knowledge [02 §6].

What this means in practice:

- **Where you launch matters.** Starting in `repo/services/api` loads the `CLAUDE.md` files at `repo/`, then `repo/services/`, then `repo/services/api/`. Starting at `repo/` loads only the top one up front. The rest arrive lazily (§3).
- **`CLAUDE.local.md` is your personal override.** It loads after the shared files in the same directory, so it wins ties. Use it for "I use pnpm, not npm" preferences that shouldn't be committed.
- **`--add-dir` does not bring that directory's instructions along** unless you set the env var above. Many people miss this.
- **Worktrees are handled.** Inside a git worktree, directories that are in the main repo but outside the worktree are skipped [02 R15]. You won't get a stale copy of the main checkout's `CLAUDE.md`.

### Editing instruction files mid-session is cheap now

On a re-read, only changed files are re-sent, marked as differing from the earlier copy. Removed files are listed as gone [02 R16]. Since 2.1.236 this delta approach means editing an instruction file no longer invalidates the whole cached conversation [Δ 16]. Fixing your `CLAUDE.md` while a session is running is fine and cheap.

## 2. `@imports`: composition, with limits

You can pull other files into a `CLAUDE.md` with `@path` [02 R17]:

- Accepted forms: `./rel/path.md`, `~/path.md`, absolute paths, and bare `name.ext`.
- `@` must follow whitespace or the start of a line. `\ ` escapes a space. Any `#fragment` is dropped.
- Imports are read from **text nodes only**. `@foo.md` inside backticks or a fenced code block is *not* an import, so writing about a file doesn't pull it in.
- HTML comments are stripped first, so `<!-- @docs/old.md -->` is a cheap way to disable an import.
- Recursion stops at **5 levels**. Cycles are detected by real path, with symlinks resolved.
- Only about 120 text extensions are importable. `@diagram.png` is ignored.
- **Imports from outside your cwd need a one-time approval.** User-level files (`~/.claude/...`) may always import external paths.

Use imports to share one source of truth, such as `@docs/conventions.md` or `@AGENTS.md`. Don't use them as a way around size limits. An import costs exactly as many tokens as pasting the file in.

## 3. Monorepos: lazy rules and nested files

Two mechanisms keep a large repo from dumping every team's instructions into every session [02 §6].

**Path-scoped rules.** A file in `.claude/rules/` whose frontmatter has `paths:` globs is **deferred**. It is loaded only when a tool touches a matching path, using gitignore semantics. A trailing `/**` is normalised away [02 R19]:

```markdown
---
paths:
  - services/billing/**
  - "**/*.sql"
---
# Billing service rules
- Money is integer minor units (`amount_cents`). Never floats.
- Every migration needs a matching down-migration in the same PR.
```

(The spec says "globs" without fixing the YAML shape. A list is the natural form. A rules file *without* `paths:` loads unconditionally on every session start [02 R15].)

**Nested `CLAUDE.md`.** When a tool reads or writes a file, the path is recorded. On the **next turn**, the harness walks from that file's directory up to the cwd and attaches any instruction files it finds there [02 R20].

Practical consequences:

- **There is a one-step lag.** Nested files arrive the turn after the first touch, so the first edit in a fresh subtree can happen before that subtree's `CLAUDE.md` is in context. For risky areas, start with "read `services/billing/README` first", or start the session inside the subtree.
- **Permissions can hide instructions.** If a `Read` deny rule covers a nested instruction file, it shows up only as *withheld*, and its content never loads [02 R20].
- **Built-in Explore and Plan subagents omit project instructions** [06 R25]. Don't rely on `CLAUDE.md` to constrain what a research subagent does. Put the constraint in your request.

## 4. Size caps and what happens over them

| Condition | Result |
|---|---|
| A file over **4 MiB** | Skipped entirely [02 R18] |
| A file over `max(40 000 chars, 5 % of the context window)` | Warning only. **Not truncated** [02 R18] |
| A combined total over `max(120 000, …)` | Warning only. **Not truncated** [02 R18] |
| `MEMORY.md` over 200 lines or 25 000 bytes | **Truncated**, with a note that only part was loaded [02 R24] |

The warning thresholds are generous. Treat them as a ceiling, not a target. Everything in your instruction files is context you pay for in every session, and it competes for attention with the actual task. Long procedures that are used only sometimes (release steps, migration playbooks) belong in a skill. Only a skill's short description sits in context, under a listing budget of about 1 % of the window, and the body loads on demand [02 R37] [02#T11].

## 5. AGENTS.md

As of this build, a built-in extension that is on by default loads `AGENTS.md` with the same discovery, precedence and placement as `CLAUDE.md` [02 R21a] [Δ 26]. It has three modes, chosen by a setting (the spec doesn't name the key):

- native file only;
- **native file *or* `AGENTS.md` (the default):** a directory with no `CLAUDE.md` contributes its `AGENTS.md` instead;
- both, de-duplicated when the contents are identical.

Earlier builds (2.1.236 and before) only detected `AGENTS.md` and never loaded it [02 R21a]. **The trap:** under the default mode, a directory with both files loads only `CLAUDE.md`. If `AGENTS.md` is your canonical cross-tool file, make `CLAUDE.md` a thin wrapper:

```markdown
@AGENTS.md

## Claude-specific
- Prefer the dedicated file tools; run `make check` before declaring done.
```

## 6. Excluding and disabling

- `claudeMdExcludes` (setting) takes globs that apply to User, Project and Local files. Excluding a parent also excludes everything it imports [02 R21]. Use it to mute another team's `CLAUDE.md` in a shared monorepo. Managed files can't be excluded this way: the setting covers only those three types.
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS` turns off instruction-file loading entirely [02 R21]. It's useful for checking whether a strange behaviour comes from your instructions.
- `CLAUDE_CODE_SIMPLE` reduces the whole prompt to just the cwd and date. It also disables auto-memory [02 R7, R23].

## 7. Writing instructions that survive compaction

Long sessions get compacted. Compaction summarises the conversation, then deterministically re-injects a budgeted set of things: up to 5 recently read files (memory and plan files excluded), invoked skills (5k tokens each, 25k total), the plan, background-task status and the deferred-tool listing [05 §4.5]. Since 2.1.236, compaction also re-states standing context such as instruction files after the summary [Δ 18], and "compaction" is a named reason for refreshing session context [02 §2.4].

So the rule is simple: **anything you need for the whole session goes in a file, not in chat.**

- **Chat instructions are summarised, not kept.** The summariser must include a list of your messages and carry over constraints exactly, especially security-relevant ones [05 R23, R24]. A summary is still a summary, and "always use `--frozen-lockfile`" said on turn 3 is at the mercy of the summariser by turn 200. If you notice yourself repeating it, move it into `CLAUDE.md` or `CLAUDE.local.md`.
- **Put critical rules in unconditional files.** Which attachments are re-emitted after compaction is only partly known [02 §8]. Path-scoped rules and nested files reload when a matching path is touched again, but a rule you *must* have from the first post-compaction step should live in a root-level file (inferred).
- **Write rules that can be checked.** "Run `pnpm test --filter <pkg>` before saying done" survives paraphrase. "Be careful with tests" doesn't.
- **Use the compaction hooks for dynamic state.** A `SessionStart` hook with source `compact` runs after every compaction and can re-inject current state (branch, failing tests, ticket). A `PreCompact` hook can add summariser instructions or block compaction [05 R16, R21] [05#T12]. Recipes are in chapter 03.
- **Steer manual compaction.** Instructions after `/compact` are merged with any PreCompact hook instructions [05 R16]. Use them to say what must survive ("keep the list of files we decided not to touch").

## 8. A good CLAUDE.md skeleton

```markdown
# <Project> — instructions for Claude

## Commands (exact, copy-pasteable)
- Build: `make build`
- Fast tests for one package: `pnpm test --filter <pkg>`
- Lint + typecheck (run before saying "done"): `make check`

## Layout
- `services/*` — independently deployed; each has its own CLAUDE.md
- `packages/shared` — public API; changes need a changeset
- `generated/` — never edit by hand; regenerate with `make gen`

## Conventions that aren't obvious from the code
- Errors: return `Result<T, AppError>`; don't throw across package boundaries.
- Time: store UTC, render in the user's zone at the edge.

## Boundaries
- Never touch `infra/prod/**` or run `terraform apply`.
- Ask before adding a dependency.

## Pointers (loaded only when needed)
- Release procedure: the `release` skill
- Deep architecture notes: docs/architecture.md (read on demand)
```

Notice what's missing: no tutorial on the language, no restating defaults Claude already follows, and no git history. Every line should change behaviour.

## 9. Auto-memory

Auto-memory is on by default. It is controlled by `autoMemoryEnabled` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (truthy turns it off, falsy forces it on) [02 R23]. It lives under your config directory, keyed by the canonical git root [02 R22], so all worktrees of a repo likely share one store (inferred from "canonical"). `autoMemoryDirectory` overrides the location [02 R22].

### Structure

- **One fact per file**, with frontmatter fields `name`, `description` and `metadata.type`. The type is `user`, `feedback`, `project` or `reference`. Feedback and project memories add a "Why" line and a "How to apply" line. Related memories are linked with `[[name]]` [02 §2.6, R25].
- **`MEMORY.md` is the index.** It has one bullet per memory: a link plus a hook of about 150 characters or less. It is always loaded and truncated at **200 lines / 25 000 bytes** [02 R24].

### How recall works, and its budgets

On each user turn, **at most 5** memory files are selected, either by local index search or by a small side-model selector [02 R28]. The selector is told to be conservative, to match on what your query is *about* rather than on shared keywords, and to be sparing with profile or overview memories [02 R28]. Each recalled file is capped at **4096 bytes / 200 lines**, and the whole session has a **61 440-byte recall budget**. That's about fifteen maximum-size memories, then recall stops [02 R28]. After a memory write, the file is checked against those limits and Claude is asked to split it if it's too big [Δ 25].

Implications:

- **Small, specific memories stretch the budget.** Ten 400-byte memories cost less than one 4 KB "everything about the project" file, and they are far more likely to be selected.
- **The `description` line is what gets matched.** Write it as the situation where the memory applies, not as a title.
- **Memories go stale.** The memory instructions treat a memory that names a code entity as true *when written*, to be verified before use [02 R25]. Don't store anything you'd rather Claude read fresh from the repo.

### What not to save

The memory rules exclude these, *even when you ask*: code patterns, git history, fix recipes, anything already in instruction files, and ephemeral task state. When asked to save one, Claude should look for the non-obvious insight behind it and save that instead [02 R25].

| Don't | Do |
|---|---|
| "The auth middleware is in `src/mw/auth.ts`" (code fact, will rot) | "User prefers explicit dependency injection over module singletons. Why: had test-isolation pain. How to apply: suggest constructor params." |
| "Fixed the flaky test by adding a retry" (fix recipe) | "The CI runners are ARM. x86-only binaries fail silently in `postinstall`." |
| "Currently refactoring billing" (ephemeral) | "Staging DB is shared with QA. Never truncate tables there." |
| A 3 KB project overview | Three 300-byte facts, each with a precise description |

Memory writes are refused if they contain invisible or control characters or text shaped like a harness tag [02 R27]. If you edit memory files by hand, Claude is told its loaded copy is stale [02 R29].

---

## Gotchas

- **The nearest file wins.** Your global `~/.claude/CLAUDE.md` loads *before* project files, so a project rule that contradicts it will usually prevail [02 R15].
- **Large files are not truncated.** A 300 KB `CLAUDE.md` loads in full every session, with just a warning. Over 4 MiB it disappears without notice [02 R18].
- **A rule in a code block is fine, but an import in a code block is ignored** [02 R17]. Keep `@path` imports in plain text.
- **Imports outside the cwd** stall on an approval the first time [02 R17]. In headless runs, keep imports inside the repo or move them to user-level files.
- **`--add-dir` doesn't load that directory's `CLAUDE.md`** unless `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is set [02 R15].
- **Nested `CLAUDE.md` arrives one turn late**, and a `Read` deny can withhold it entirely [02 R20].
- **Explore and Plan subagents don't see project instructions** [06 R25].
- **Having both `AGENTS.md` and `CLAUDE.md`** in one directory loads only `CLAUDE.md` by default [02 R21a].
- **Overgrown `MEMORY.md`** is cut at 200 lines / 25 KB, and anything past the cut is invisible [02 R24]. Prune it, or ask Claude to consolidate.
- **Memory is keyed by the canonical git root, not by branch** [02 R22]. Project memories written on one branch show up on every branch, and very likely in every worktree too (inferred).
