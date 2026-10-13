# 02 — System prompt and context assembly (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

## 1. Purpose & scope

This document specifies how the engine assembles the model's instructions. It covers:

- the cache-scoped system prompt and its section registry;
- the per-turn "attachment" pipeline that carries all volatile context as reminder messages;
- discovery of instruction files (CLAUDE.md and rules);
- auto-memory;
- output styles;
- prompt overrides from the CLI and SDK;
- the bounds applied to every injected blob.

The wording of the prompts is deliberately not specified. Only each section's purpose and required content is described, and implementers should write their own text.

## 2. Concepts & data model

### 2.1 Prompt section
```
Section:
  id:          str            # stable identity, e.g. "communication"
  variant_key: str            # id plus ":" plus variant qualifiers; used as memo key
  scope:       "shared" | "session"
  compute:     async (facts) -> str | None    # None = omit for this model/config
  cache_break: bool = False   # recompute every time if True
```
Sections are memoized for the session by `variant_key`. The id is `variant_key` with everything from the first `:` removed.

### 2.2 System prompt blocks (as sent)
```
[ billing/attribution header block   cache: none            ]
[ identity prefix block              cache: none or org     ]
[ shared sections joined             cache: global scope    ]
  <boundary sentinel – stripped before sending>
[ session sections joined            cache: org scope       ]
```
If the boundary is absent, every block is org-scoped. The boundary is emitted only for providers and configurations that support scoped caching.

### 2.3 Attachment
```
Attachment:
  type: str          # e.g. environment, model, date, session_context, instructions,
                     # nested_memory, relevant_memories, todo_reminder, plan_mode, ...
  payload: dict
```
Attachments are persisted in the transcript JSONL as `type: "attachment"` entries. Only at serialization time are they rendered to text inside `isMeta` user messages, wrapped in `<system-reminder>` … `</system-reminder>`.

### 2.4 Key attachment payloads
```
environment: {
  snapshot: {workingDirectory, isWorktree, isGitRepo, additionalWorkingDirectories,
             platform, shell, osVersion, scratchpadDirectory?, githubCli?},
  changes?: [{field, from?}]           # present only on updates
}
model:           {marketing_name, model_id, knowledge_cutoff}
date:            {date} | {date, changed: true}
session_context: {userEmail?, attachedProject?, gitStatus?, perforceMode?, refresh_reason?}
instructions:    {files: [{path, type, content}], removed?: [path], changed?: bool, reason?}
nested_memory:   {path, content}
withheld_memory: {path}
relevant_memories: [{path, age, content}]
```
The refresh reasons are `session_start`, `compaction`, `policy_refresh`, `directory_added`, `settings_sync`, `account_change`, `hooks_invalidate`, `policy_verdict`, `memory_paused`, `memory_resumed`, `auto_memory_off` and `auto_memory_back_on`.

### 2.5 Instruction file
```
InstructionFile: {path, type: Managed|User|Project|Local|AutoMem, content, conditional_globs?: [glob]}
```

### 2.6 Memory file (on disk, markdown with frontmatter)
```
---
name: <short name>
description: <one line>
metadata:
  type: user | feedback | project | reference
---
<one fact; for feedback/project add a "Why" line and a "How to apply" line; link peers with [[name]]>
```
`MEMORY.md` is the index. It holds one bullet per memory: a markdown link to the file plus a short hook, about 150 characters or less.

## 3. Behavioural requirements

### System prompt
- **R1 Identity prefix.** There MUST be three identity-prefix variants:
  - interactive CLI (also used for some cloud providers);
  - SDK-hosted with an appended prompt;
  - generic SDK agent with no append.

  Non-interactive sessions choose between the second and third by whether an append prompt exists. On resume, the prefix recorded for the session is reused. *Confidence: observed.*
- **R2 Block splitting.** Header strings carrying billing attribution are uncached. The identity prefix is its own block. Text before the boundary is joined into one global-scope block, and text after it is org-scoped. A missing boundary SHOULD be logged. An outcome-reporting block, when present, is kept uncached. *Confidence: observed.*
- **R3 Shared sections.** The shared (static) part MUST contain these sections, in order. *Confidence: observed.*

  | Section | Purpose |
  |---|---|
  | intro | Agent identity and purpose. Security-work policy: help with authorised and defensive work, refuse destructive techniques. A rule against inventing URLs. |
  | system | Output is markdown shown in a monospace terminal. Tools run under a permission mode, and a denied call should not be retried unchanged. Tags carry system information. Suspected prompt injection should be flagged. Hook feedback counts as user feedback. Old context is compressed automatically, so the conversation is not bounded by the window. |
  | task conduct | Scope discipline: no speculative features or abstractions, no unneeded error handling, validation only at system boundaries. Comments by default only when the reason is non-obvious. Exercise UI changes in a browser before claiming done. For exploratory questions, give a brief recommendation with the main trade-off and wait for agreement. |
  | action caution | Judge actions by reversibility and blast radius. Name the classes needing confirmation: destructive, hard to undo, visible to others, uploads to third parties. A one-time approval is not standing approval. No bypassing verification hooks. Inspect state before discarding work. |
  | tool usage | Prefer dedicated file tools over the shell. How to use the todo tool, marking items done immediately. Issue independent calls in parallel. |
  | tone | No emojis unless asked. Concise. Cite `path:line`. No colon right before a tool call. |

  The "task conduct" section MUST be omitted when an output style is active that does not opt to keep coding instructions. A "lean" model trait MAY replace all six with a single compact harness section.
- **R4 Session sections.** The session (dynamic) part is a list of independently computed sections, each of which can be null for a given model or config. Topics include:
  - how to communicate with a user who sees only text output, not tool calls;
  - pronouns;
  - action caution;
  - task continuity;
  - tool-parameter JSON formatting;
  - session-specific guidance;
  - the memory instructions;
  - simple or static environment info;
  - background-session notes;
  - context-management assurance;
  - focus or brief modes;
  - delegation steering;
  - an autonomy check;
  - total-token and cost notes.

  *Confidence: observed (some sections' content is delivered remotely and was not seen).*
- **R5 Memoization.** All session sections MUST be computed in parallel and memoized for the session by variant key. A section is recomputed only when it is flagged as cache-breaking or when sections are explicitly invalidated. *Confidence: observed.*
- **R6 Host override seams.** The engine MUST expose two hooks:
  - (a) a per-section resolver that lets a host rewrite any section by id;
  - (b) a compose resolver that replaces the whole build.

  Hosts receive a "facts" object to recompose from: model, tools, output style, and trait flags (bare, lean, sdk-preset, teammate, analysis, print, skills, send-user-message). *Confidence: observed.*
- **R7 Bare mode.** With `CLAUDE_CODE_SIMPLE` set, the whole prompt MUST collapse to the working directory and the date. *Confidence: observed.*
- **R8 SDK preset.** With dynamic sections excluded:
  - drop the memory section;
  - use static rather than dynamic env info;
  - add a static memory section to the shared part, so the prompt is byte-identical across machines.

  Excluded dynamic content is supplied separately. Each such body MUST begin with a `# <heading>` line, and an error is raised otherwise. *Confidence: observed.*
- **R9 Mid-conversation system turns.** If the model supports them, the system prompt MUST say that updates may arrive as system turns mid-conversation. Otherwise it MUST say that reminder tags are unrelated to the message or tool result they happen to appear in. *Confidence: observed.*

### Volatile context as attachments
- **R10 Environment.** The environment MUST be delivered as an `environment` attachment, not as a system section. The engine compares the new snapshot with the latest one in the transcript:
  - identical: emit nothing;
  - different: emit an "environment update" listing each changed field with its old value.

  In a worktree, two warnings are added: that this is an isolated worktree, and that the git stash stack is shared with other checkouts. The scratchpad directory and a rule about treating downloads as untrusted MAY be included. *Confidence: observed.*
- **R11 Model identity.** A `model` attachment states the marketing name, the exact model id and the knowledge cutoff. It is re-sent only when the identity changes, for example on a model switch or a plan-mode model swap. *Confidence: observed.*
- **R12 Session context.** The attachment opens with a line introducing it as context for answering. It lists the keys present, and closes by saying the context was attached automatically and need not be reported back. A refresh says the values replace the earlier ones and names the reason (§2.4). *Confidence: observed.*
- **R13 Git snapshot.** The engine runs these in parallel:
  - short status, without taking optional locks;
  - the current branch;
  - the main branch;
  - the last 5 commits in one-line form;
  - the configured user name.

  Status is truncated at 2000 characters, with a note to run `git status` for the rest. The snapshot is skipped in remote mode, and disabled by `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS` or the `includeGitInstructions` setting. *Confidence: observed.*
- **R14 Date.** A `date` attachment states today's date. When the date rolls over, a variant says the date has changed and should not be announced. *Confidence: observed.*

### Instruction files
- **R15 Load order.** Files are loaded in the order below. Later entries appear later in context and therefore carry more weight. *Confidence: observed.*
  1. Managed: the policy-directory `CLAUDE.md`, a remotely managed string, the policy-settings `claudeMd` value, and managed `.claude/rules/`.
  2. User: `~/.claude/CLAUDE.md` and `~/.claude/rules/**/*.md`.
  3. Project and Local: for each directory from the filesystem root down to the cwd, load `CLAUDE.md`, `.claude/CLAUDE.md`, the unconditional `.claude/rules/**/*.md`, and `CLAUDE.local.md`. In a git worktree, skip directories that are inside the main repo but outside the worktree.
  4. `--add-dir` directories, only if `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is truthy.
  5. AutoMem: the auto-memory `MEMORY.md` index.
- **R16 Rendering.**
  - The block begins with a preamble saying these instructions override default behaviour and must be followed exactly.
  - Each file is introduced with its path and a provenance label: project instructions checked into the repo; private project instructions; auto-memory; organisation policy; or private global instructions.
  - Delivery is an `instructions` attachment, never the system prompt.
  - On a re-read, only changed files are re-sent, with a note that they differ from earlier copies, and removed files are listed as no longer present.

  *Confidence: observed.*
- **R17 Imports.**
  - Parse the file as GFM markdown, after stripping HTML comments.
  - Take `@path` tokens only from text nodes, never from code spans or blocks. A token is `@` preceded by start of text or whitespace and followed by non-space characters, where `\ ` escapes a space. Drop any `#fragment`.
  - Accept `./`, `~/`, absolute paths, and bare `name.ext`.
  - Allow at most 5 levels of recursion and only an allowlist of about 120 text extensions.
  - Detect cycles with a set of visited real paths, symlinks resolved.
  - Includes from outside the cwd need a one-time approval. User-level files may always include external paths.

  *Confidence: observed.*
- **R18 Size limits.** Files over 4 MiB are skipped. A file over `max(40 000 chars, 5 % of the context window × bytes per token)` triggers a warning, as does a total over `max(120 000, …)`. Warnings MUST NOT truncate. *Confidence: observed.*
- **R19 Conditional rules.** A rules file with `paths:` frontmatter (globs, with a trailing `/**` normalised away) MUST be deferred. It is loaded when a tool touches a matching path, matched with gitignore semantics. *Confidence: observed.*
- **R20 Nested instruction files.** When a tool reads or writes a file, record its path. On the next turn, walk from that file's directory up to the cwd and emit `nested_memory` attachments for any instruction files found. Files that permissions deny become `withheld_memory`. *Confidence: observed.*
- **R21 Exclusions.** The `claudeMdExcludes` setting holds globs that apply to User, Project and Local files. An excluded parent also excludes its imports. `CLAUDE_CODE_DISABLE_CLAUDE_MDS` disables loading entirely. AGENTS.md is detected but not loaded. *Confidence: observed (no load path found for AGENTS.md).*
- **R21a Cross-tool instruction file.** A built-in, default-on extension loads the cross-tool `AGENTS.md` file, using the same discovery, precedence and placement as `CLAUDE.md`. It has three modes, selectable by setting: native file only; native file **or** `AGENTS.md` (the default, where a directory with no `CLAUDE.md` contributes its `AGENTS.md` instead); and both, de-duplicated when the contents match. Earlier builds only detected the file (for read and edit protection) and never loaded it. Confidence: observed.

### Auto-memory
- **R22 Location.** The default location is `<configDir>/projects/<sanitized canonical git root>/memory/`. It can be overridden by the `autoMemoryDirectory` setting, read from policy, then flag, then trusted local or project settings, then user settings. *Confidence: observed.*
- **R23 Enablement.** Auto-memory is on by default. `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (truthy turns it off, falsy forces it on) and the `autoMemoryEnabled` setting control it. It is off in bare mode, and off in remote mode unless `CLAUDE_CODE_REMOTE_MEMORY_DIR` is set. *Confidence: observed.*
- **R24 Index truncation.** `MEMORY.md` MUST be truncated to 200 lines or 25 000 bytes, whichever comes first, with an appended warning that only part was loaded. *Confidence: observed.*
- **R25 Memory section content.** The memory section MUST cover:
  - the memory location;
  - one fact per file, using the frontmatter schema in §2.6;
  - "why" and "how to apply" lines for feedback and project memories;
  - linking related memories;
  - a short index entry for every file;
  - a list of things not to save, which applies even when the user asks: code patterns, git history, fix recipes, anything already in instruction files, ephemeral task state. When the user asks to save one of these, the model should instead probe for the unexpected insight behind the request and store that;
  - a staleness rule: a memory naming a code entity was true when written, and must be verified before it is relied on.

  *Confidence: observed.*
- **R26 Team memory.** Team memory stores (`CLAUDE_MEMORY_STORES`) mount under `memory/team/<mount>/`. Path handling MUST reject null bytes, encoded `..`, Unicode-normalisation tricks and symlink escapes. *Confidence: observed.*
- **R27 Write lint.** A write to a memory file MUST be refused if it contains invisible or control characters, or text shaped like a harness tag. Background writers strip such content instead of refusing. *Confidence: observed.*
- **R28 Recall.** On each user turn, select at most 5 memory files, using one of two strategies:
  - (a) local index search with a relevance floor, fetching `5 + min(surfaced + 10, 20)` candidates;
  - (b) a side-model selector: the main model, plus up to 2 cheaper fallbacks; `max_tokens` 512; JSON-schema output with a list of selected memories. Its cached system prompt asks it to be conservative, to match on what the query is about rather than on keyword overlap, to be especially sparing with profile or overview memories, and not to re-select earlier picks.

  Each recalled file is capped at 4096 bytes or 200 lines. The session recall budget is 61 440 bytes. Each recalled memory's header shows its path and age. *Confidence: observed.*
- **R29 External changes.** External edits to the memory directory MUST produce a reminder that a loaded copy is stale. *Confidence: observed.*

### Attachment pipeline
- **R30 Wrapping.** Every text block of an `isMeta` user message, and all hook output, MUST be wrapped in `<system-reminder>` tags. *Confidence: observed.*
- **R31 Producers.** At each turn boundary, run all producers (about 50) in parallel under one shared 1000 ms abort deadline. A failing producer contributes nothing. Durations are sampled at about 5 %. `CLAUDE_CODE_DISABLE_ATTACHMENTS` limits the set to queued commands, the agent listing, sandbox, environment, model and settings. *Confidence: observed.*
- **R32 Throttled nudges.** The rules for each nudge type are in the table below. *Confidence: observed.*

  | Type | Rule |
  |---|---|
  | `todo_reminder` | Requires ≥ 10 assistant turns since the last todo-tool use and ≥ 10 since the last reminder. Includes the current list and calls itself a gentle reminder to ignore if not applicable. Mode set by `CLAUDE_CODE_TODO_REMINDER_MODE`. |
  | `plan_mode` | At most once per 5 turns while plan mode is on. Every 5th one is the full version; the others are sparse. Separate re-entry and exit variants exist. The full version forbids edits other than to the plan file and lays out phases: explore with search agents, design with planning agents, review, write the plan, then end only by asking the user or by exiting plan mode. |
  | `edited_text_file` | A file read in full has a newer mtime than its read. Re-send a diff snippet within a total snippet budget, otherwise just the notice. The wording treats the change as intentional and current rather than something to revert. |
  | `total_tokens_reminder` | Renders `<total_tokens>N tokens left</total_tokens>`. Modes: `countdown`, `padded-countdown` (default), `fixed`, `infinite`, `off`, set by `CLAUDE_CODE_TOTAL_TOKENS_REMINDER[_BUDGET]` or settings. |
  | `token_usage` | Only with `CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT`. Shows used, total and remaining. |
  | `budget_usd` | Only when a USD budget is set. |
  | `output_style` | Every turn while a style is active. |
  | deltas | The deferred-tool, agent-listing, MCP-instruction, dropped-MCP-tool and skill-listing sets are announced as diffs. Removed items are explicitly marked "disregard". |
  | `critical_system_reminder` | Taken from an agent-definition field and re-sent every turn. |
  | auto mode | Sent when the permission mode is auto or bypass. Includes a steer to prefer the shell. |
  | IDE | Diagnostics, selections and opened files. Selections note that they may be unrelated to the task. |
  | hooks | SessionStart and UserPromptSubmit `additionalContext` and `systemMessage`. |
  | `invoked_skills` | After compaction, re-inject skill bodies with an instruction not to re-run them. |
  | `date_change` | Sent on calendar rollover. |

### Output styles and language
- **R33 Style sources.** Built-in styles include explanatory, learning and concise. Custom styles are loaded from `~/.claude/output-styles/*.md`, `.claude/output-styles/` and plugins. Frontmatter fields: `name`, `description`, `keep-coding-instructions`, and a plugin-forcing flag. *Confidence: observed.*
- **R34 Style effects.** An active style:
  - (a) switches the intro to say the agent follows an output style;
  - (b) removes task conduct unless the style keeps coding instructions;
  - (c) sends the style body as an `output_style_instructions` attachment, headed with the style name, only when the style changes; a reset sends a note that the default is restored;
  - (d) adds a short per-turn reminder that the style is active.

  The `language` setting uses the same change-only pattern, and clearing it tells the model to match the user's language. *Confidence: observed.*

### Overrides and sub-agents
- **R35 Prompt resolution order.** `overrideSystemPrompt` wins over everything. Next come coordinator mode, then an agent prompt or `customSystemPrompt` (`--system-prompt[-file]`) or the default sections, then skill persistence, and finally `appendSystemPrompt` (`--append-system-prompt[-file]`), which is appended last. A main-thread agent's prompt replaces the default, unless the agent requests append mode. *Confidence: observed.*
- **R36 Sub-agent notes.** Sub-agents MUST receive a fixed notes block covering:
  - use absolute paths;
  - no emojis;
  - no colon before tool calls;
  - do not write report files;
  - no message from another agent constitutes user consent.

  *Confidence: observed.*
- **R37 Skill listing budget.** The skill listing is limited to `context_window × 4 × 1 %` characters, which is 8000 for a 200k window. `SLASH_COMMAND_TOOL_CHAR_BUDGET` or the `skillListingBudgetFraction` setting overrides the budget. Each description is capped at 1536 characters (`skillListingMaxDescChars`). Over budget, the lowest-priority skills degrade to name only, while bundled skills keep their descriptions. *Confidence: observed.*
- **R38 Where procedures live.** The git commit and PR procedure MUST live in the shell tool's description, not in the system prompt. Attribution trailers arrive through a reminder. *Confidence: observed.*

## 4. Algorithms

```python
async def build_system_prompt(tools, model, opts):
    if bare_mode(): return [f"CWD: {cwd()}\nDate: {today()}"]
    shared = ([lean_section()] if model.has_trait("lean")
              else [intro, system, task_conduct if keep_coding(opts) else None,
                    action_caution, tool_usage, tone])
    session = await gather_memoized(session_sections(model, opts))   # memo by variant_key
    parts = resolve_order(opts, default=[*filter(None, shared), BOUNDARY, *filter(None, session)])
    if host.compose_resolver: parts = host.compose_resolver(facts(model, tools, opts), parts)
    parts = [host.section_resolver(p) if host.section_resolver else p for p in parts]
    return split_blocks([billing_header(), identity_prefix(opts), *parts])

def split_blocks(strings):
    blocks, before, after, seen_boundary = [], [], [], False
    for s in strings:
        if s.startswith(BILLING_HEADER_PREFIX): blocks.append(Block(s, cache=None))
        elif s is IDENTITY: blocks.append(Block(s, cache=None))
        elif s == BOUNDARY: seen_boundary = True
        else: (after if seen_boundary else before).append(s)
    if not seen_boundary:
        log_missing_boundary(); return blocks + [Block("\n\n".join(before), cache="org")]
    return blocks + [Block("\n\n".join(before), cache="global"),
                     Block("\n\n".join(after), cache="org")]

async def compute_attachments(ctx):
    deadline = AbortAfter(ms=1000)
    async def safe(p):
        try: return await p(ctx, deadline)
        except Exception: return []
    results = await asyncio.gather(*(safe(p) for p in active_producers(ctx)))
    return [a for r in results for a in r]

def environment_producer(ctx):
    snap = environment_snapshot()
    prev = latest_attachment(ctx.transcript, "environment")
    if prev is None: return [Attachment("environment", {"snapshot": snap})]
    changes = [{"field": f, "from": prev.snapshot[f]} for f in snap if snap[f] != prev.snapshot.get(f)]
    return [Attachment("environment", {"snapshot": snap, "changes": changes})] if changes else []

def todo_producer(ctx):
    if ctx.turns_since_todo_write >= 10 and ctx.turns_since_todo_reminder >= 10:
        return [Attachment("todo_reminder", {"items": ctx.todos})]
    return []

def plan_mode_producer(ctx):
    if not ctx.plan_mode or ctx.turns_since_plan_reminder < 5: return []
    ctx.plan_reminder_count += 1
    full = ctx.plan_reminder_count % 5 == 1        # first and every 5th thereafter
    return [Attachment("plan_mode", {"full": full})]

def discover_instruction_files(cwd, settings):
    files = managed_files() + user_files()
    for d in path_chain(root(), cwd):              # root first, cwd last
        if in_main_repo_outside_worktree(d): continue
        for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
            files += load(d / name, "Project")
        files += [r for r in load_rules(d / ".claude/rules") if not r.conditional_globs]
        files += load(d / "CLAUDE.local.md", "Local")
    if env_truthy("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"):
        for d in added_dirs(): files += project_files_in(d)
    files += [load_memory_index()]                 # AutoMem, truncated 200 lines / 25 000 B
    files = [f for f in files if not excluded(f, settings.get("claudeMdExcludes"))]
    return [expand_imports(f, depth=0, seen=set()) for f in files if size(f) <= 4 * 2**20]

def expand_imports(f, depth, seen):
    if depth > 5 or realpath(f.path) in seen: return f
    seen.add(realpath(f.path))
    for tok in at_tokens_in_text_nodes(strip_html_comments(f.content)):
        p = resolve(tok.split("#")[0].replace("\\ ", " "), base=f.path.parent)
        if p.suffix not in TEXT_EXTENSIONS: continue
        if outside(p, cwd()) and f.type != "User" and not external_includes_approved(): continue
        f.children.append(expand_imports(load(p, f.type), depth + 1, seen))
    return f

def recall_memories(query, ctx):
    if ctx.recall_bytes_used >= 61_440: return []
    picks = selector(query, ctx.memory_index, exclude=ctx.already_recalled)[:5]
    out = []
    for p in picks:
        body = head(read(p), max_bytes=4096, max_lines=200)
        if ctx.recall_bytes_used + len(body) > 61_440: break
        ctx.recall_bytes_used += len(body); out.append((p, age(p), body))
    return out

def skill_listing(skills, window=200_000, frac=0.01):
    budget = int(env("SLASH_COMMAND_TOOL_CHAR_BUDGET") or window * 4 * frac)
    entries = [(s, s.description[:1536]) for s in skills]
    for s, _ in sorted(entries, key=priority_ascending):
        if total_len(entries) <= budget: break
        if not s.bundled: set_description(entries, s, None)   # degrade to name only
    return entries
```

## 5. Constants & knobs

| Item | Value |
|---|---|
| Attachment producer deadline | 1000 ms (shared) |
| Producer timing sample rate | ~5 % |
| Git status cap | 2000 chars |
| Git log depth | 5 commits |
| Import recursion depth | 5 |
| Instruction file hard skip | 4 MiB |
| Per-file warn / total warn | max(40 000 chars, 5 % of window) / max(120 000, …) |
| MEMORY.md cap | 200 lines / 25 000 bytes |
| Recalled memories per turn | 5 |
| Recalled file cap | 4096 bytes / 200 lines |
| Session recall budget | 61 440 bytes |
| Selector max_tokens / fallbacks | 512 / 2 |
| Index-search candidates | 5 + min(surfaced + 10, 20) |
| Memory index line length | ~150 chars |
| Todo reminder | 10 turns since write and 10 since last reminder |
| Plan-mode reminder | every 5 turns; full every 5th |
| Skill listing budget | window × 4 × 0.01 chars; 1536 per description |
| Env vars | `CLAUDE_CODE_SIMPLE`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS`, `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`, `CLAUDE_MEMORY_STORES`, `CLAUDE_CODE_DISABLE_ATTACHMENTS`, `CLAUDE_CODE_TODO_REMINDER_MODE`, `CLAUDE_CODE_TOTAL_TOKENS_REMINDER[_BUDGET]`, `CLAUDE_CODE_ENABLE_TOKEN_USAGE_ATTACHMENT`, `SLASH_COMMAND_TOOL_CHAR_BUDGET` |
| Settings | `includeGitInstructions`, `claudeMdExcludes`, `autoMemoryDirectory`, `autoMemoryEnabled`, `language`, `skillListingBudgetFraction`, `skillListingMaxDescChars`, policy `claudeMd` |

## 6. Rationale

- **Static system prompt, volatile messages.** Keeping volatile facts out of the system prompt means a cwd change or a new day does not invalidate a large cached prefix that is shared globally.
- **Diffs over rewrites.** Append-only diff attachments keep history immutable, so the cache stays valid and hot reloads are cheap to express.
- **Memoization by variant key.** This aligns prompt identity with cache identity and gives embedding hosts a clean override seam.
- **Deadlines and silent failure.** These keep a slow integration from stalling the turn.
- **Provenance labels and load order.** They let the model resolve conflicts between instruction sources.
- **Lazy rules and nested files.** These scale instructions to large monorepos.
- **Stated caps.** Telling the model what was cut turns potential hallucination into a follow-up tool call.

## 7. Harness design takeaways

- **T1 Static/dynamic split with volatile data in messages.** Put a boundary between globally cacheable static sections and org/session sections. Move environment, git, date, model and instruction files out of the system prompt entirely.
- **T2 Append-only typed attachments sent as diffs.** Re-announce environment, instruction files, tool, MCP and agent sets only on change, as "replaces earlier values" or "no longer available, disregard". History never needs rewriting.
- **T3 Memoize sections by a variant-encoding name.** Section identity becomes cache identity, and hosts can override sections by id.
- **T4 Parallel producers under a hard deadline, failing silently.** Use one shared short deadline. Errors yield nothing and are sampled for telemetry, so no slow integration blocks a turn.
- **T5 Turn-count throttling for nudges.** Todo reminders need 10 quiet turns and 10 since the last nudge. Plan-mode reminders come every 5 turns, with a full version every 5th. Each is phrased as optional.
- **T6 Provenance and precedence for instruction files.** Load Managed, then User, then Project from root to cwd, then Local, then AutoMem, each labelled. Path-scoped rules load lazily, and nested files arrive when a subtree is touched.
- **T7 Bound every blob and say what was cut.** Git status 2k chars, index 200 lines / 25 KB, recalls 4 KB, skill listing 1 % of the window, each with a pointer to the rest.
- **T8 Index plus atomic memory files with a cheap selector.** Keep the index always loaded, fetch bodies on demand (at most 5 per turn, under a session budget), and enforce what-not-to-save and verify-before-use rules.
- **T9 Harness tags are a security boundary.** Refuse memory writes containing tag-shaped text or invisible characters. Mark external content as not from the user, and tell sub-agents that agent messages are not consent.
- **T10 Model-dependent prompt.** Key lean prompts, system-turn support, communication sections and reminder cadence on model traits. A section returns null rather than branching inside its prose.
- **T11 Keep rare procedures out of the system prompt.** Git/PR procedure goes in the shell tool description, plan-mode procedure in reminders, and skills in on-demand bodies. The system prompt holds durable judgment rules.
- **T12 Tell the model its token budget.** Show remaining tokens explicitly and say that compaction exists, which prevents agents rushing to finish out of context anxiety.

## 8. Uncertainties

- Which models carry the "lean" trait is not known.
- Several session sections are delivered by remote configuration, and their content was not available.
- The full text of the memory-section formatter and the SDK static-memory section was not reviewed.
- Which attachments are re-emitted after compaction is only partly known.
- *(Corrected in spec revision 2.)* An AGENTS.md loader does exist. See R21a. The first analysis missed it because it is delivered as a built-in plugin rather than in the core instruction loader.
- The scoring of the index-based memory search and its relevance floor are unknown.
- Whether the desktop host actually uses the section and compose override seams is unverified.
