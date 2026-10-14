# Using Claude Code well

Practical advice drawn from how Claude Code works inside. Every tip cites the requirement in [`../spec/`](../spec/INDEX.md) that backs it, so you can check the reasoning instead of trusting folklore.

> Behaviour as of CLI 2.1.295 (October 2026). It changes often; see [how it evolved](../spec/CHANGES-2.1.236-to-2.1.295.md). Advice that follows from the spec without being stated in it is marked *(inferred)*. Unofficial, and not affiliated with Anthropic.

**Citation key:** `[02 R21a]` = spec file 02, requirement R21a · `[04#T9]` = takeaway 9 · `[05 §4.1]` = section · `[Δ16]` = item 16 in the version changelog.

## Chapters

| # | Chapter | Read it if… |
|---|---|---|
| 01 | [Instructions & memory](01-instructions-and-memory.md) | your CLAUDE.md "isn't being followed", or you work in a monorepo |
| 02 | [Permissions & settings](02-permissions-and-settings.md) | you're tired of approval prompts, or nervous about too few |
| 03 | [Hooks recipes](03-hooks-recipes.md) | you want something to *always* happen, whatever the model remembers |
| 04 | [Context, cost & long sessions](04-context-cost-and-long-sessions.md) | sessions get slow, expensive or forgetful |
| 05 | [Tools & long-running work](05-tools-and-long-running-work.md) | builds, dev servers, flaky edits, many MCP servers |
| 06 | [Agents, skills & parallel work](06-agents-skills-and-parallel-work.md) | you want to delegate, parallelise or package workflows |
| 07 | [Agent-friendly repos](07-agent-friendly-repos.md) | you want to shape your codebase so agents do well in it |

## The top 20

**Instructions**
1. **Use path-scoped rules in big repos.** `.claude/rules/*.md` files with `paths:` frontmatter stay out of context until Claude touches a matching file [02 R19].
2. **Nested `CLAUDE.md` files arrive one turn late.** They load after Claude first touches that subtree. For risky areas, tell Claude to read the area's instructions first [02 R20].
3. **Oversized instruction files aren't truncated.** They cost you every token. Above 4 MiB they're silently skipped [02 R18].
4. **`AGENTS.md` loads by default** in any directory without a `CLAUDE.md`. If you keep both, import one from the other [02 R21a].
5. **Put durable constraints in your own messages or in CLAUDE.md**, never only in tool output. Summaries keep what users said verbatim, and instruction files are re-stated after compaction [05 R24] [Δ18].

**Permissions & hooks**

6. **Deny beats ask beats allow, whichever file a rule comes from** [04 §2.3]. One deny rule is a real fence.
7. **Don't write allow rules for read-only commands.** `ls`, `cat`, `git log/diff`, `grep` and similar are already auto-allowed [04 §4.3].
8. **In auto mode, broad rules like `Bash(python:*)` are suspended** so the safety classifier sees those calls [04 R11]. Don't expect them to unlock autonomy.
9. **Hooks fail open unless you say otherwise.** Use exit 2 to block, and set `onFailure: "block"` for guards that matter [04 R33].
10. **Narrow every hook with `matcher` and `if`** so it only starts when relevant. The default timeout is long (600 s), so set your own [04 R31–R36].

**Context & cost**

11. **Avoid long idle gaps mid-task.** On a subscription the main thread's prompt cache lasts about an hour, and with an API key about 5 minutes. After it expires you pay full price to re-send everything [01 R44].
12. **Editing CLAUDE.md mid-session is cheap now.** Changes arrive as change-only updates instead of rewriting the cached prefix [Δ16].
13. **Auto-compaction fires at about 83% of a 200k window.** Afterwards Claude gets back a summary, up to 5 recent files, the skills it used and your plan, and must re-read anything else [05 §4.1, §4.5].
14. **Use side chats for quick questions.** They fork the conversation without adding to the transcript [05 R44].

**Tools & work style**

15. **Ask for independent reads and searches in one go.** Read-only calls run in parallel, up to 10 at once [03 R9–R14].
16. **Every Bash call is a fresh shell.** The working directory carries over; `export` doesn't. Persist environment through the env-file mechanism or put setup in scripts [03 R38–R39].
17. **Never ask Claude to poll with `sleep`.** Long commands move to the background and report back when done [03 R40–R42].
18. **Ten MCP servers are fine.** Their tools load on demand through tool search, and outputs are capped at about 25k tokens [03 R54–R56].

**Delegation & repos**

19. **Delegate to keep your context clean.** A subagent's 50 file reads cost you one summary, and it knows only what you put in its brief [06 R16]. Give parallel writers `isolation: worktree` [06 R10]. Note: Explore and Plan agents don't see CLAUDE.md [06 R15].
20. **Make your repo legible.** Document one fast, deterministic test command in CLAUDE.md, prefer files under about 2,000 lines (a single read stops there), put tribal knowledge in scripts, and fence secrets with sandbox and read denies rather than polite requests [03 R48] [04 R41–R44].
