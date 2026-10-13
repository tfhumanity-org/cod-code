# How the harness evolved: Claude Code 2.1.236 → 2.1.295

Clean-room functional summary. It was derived from a static comparison of the two shipped CLI builds, and contains no original source, prompt or message text. Items that appear to concern unreleased products or capabilities are deliberately left out.

**Scope.** 59 builds apart. The bundle grew from one 27.7 MB script to about 44.7 MB, split across roughly 2,600 lazily loaded modules. The public surface barely moved:
- The built-in tool set is almost the same: one blocking "wait for output" tool was swapped for a non-blocking status check, and two narrow tools were added.
- Hook events went from 31 to 33, adding events before and after a model switch.

The real changes are underneath, and they cluster around four themes: **reliability, cache economics, safety depth and multi-agent hygiene.**

Each item is a behaviour change, followed by *why it matters* for anyone building a harness. Related takeaways in the main spec are noted as `NN#Tk`.

---

## 1. Loop, streaming and the model API

1. **The retry policy for streams that fail part-way became a declarative table.** Failures are classified by cause (overload, stall, truncation, lost connection, system suspend, server error, filtered output) and by how far the stream got (nothing, thinking-only, partial, real output). Each pair maps to one of five actions: retry, retry without streaming, keep the partial output, fall back to another model, or fail. *A pure function over (cause, progress, counters) can be unit-tested and reviewed; retries scattered through catch blocks drift and double-bill.* (01#T6)
2. **Output the user has already seen is kept rather than regenerated.** Only a malformed request or an unknown error discards it. Streams that produced only thinking get a small separate retry allowance. *Treat "the user has seen output" as a commit point.*
3. **There is now a watchdog for "no response headers at all", separate from the stream-idle watchdog.** Its window scales with request size, it is capped below the overall timeout, and it gets one dedicated retry outside the normal budget. *A request that never got an answer and a stream that went quiet are different failures, so give each its own timer.*
4. **Timeouts are sleep-aware.** Watchdogs compare monotonic time with wall time. If the machine slept through the window, the failure is classed as a suspend and retried from its own budget, instead of being counted as a server timeout. *Otherwise every closed laptop lid looks like an outage.*
5. **One attempt budget per request**, shared by stream retries, the non-streaming fallback and helper calls. *Nested retry loops multiply; a shared budget keeps the worst case bounded.*
6. **Rejected optional API features fix themselves for each feature and each conversation.** The harness drops just the rejected feature, retries, and stops sending it for the rest of the conversation. For an unexplained "bad request" it can test one suspected feature, then roll back. *Gateways differ. Degrade per feature, and latch the result so the cache key stays stable.* (01#T10)
7. **Each recovery path matches its failure.** A response cut off mid-answer gets a "resume exactly here" nudge, while subagents are asked to rewrite in full. Nudges also cover output limits hit mid-reasoning and mid-table or mid-code-block. The user sees "recovering n of 3". *Recovery should be visible, not look like a hang.*
8. **Synthetic tool results explain themselves.** Placeholders state the reason: skipped because a sibling was denied, interrupted because the session ended (so verify before re-running), or ended early to deliver a new message. *"Missing result" is ambiguous; a stated reason prevents both blind re-execution and false refusals.* (01#T5)
9. **A stop by a safety layer is now an explicit outcome for each tool call.** Calls that never ran are marked not-run, calls that were running are marked possibly partial, and the model is told this was not a tool error. A one-time retry on the same model is allowed.
10. **Model facts come from a layered catalog:** a local override, then server-provided capabilities, then a bundled catalog that can be refreshed from a signed source. Only admin settings can change that source. *Hard-coded model facts mean a release for every model change.*
11. **Effort handling is capability-driven.** Effort is reduced when thinking is off only for models that declare they need it. A single maximum-effort clamp combines the user's and the organisation's limits. Effort carries across model fallbacks.
12. **Structured output fails soft.** A lint at startup flags JSON schemas that can never be satisfied. If a backend rejects native structured output, the harness remembers that and falls back to tool-enforced output.
13. **Parallel tool runs merge their context changes at a defined point:** after the parallel batch and before the next serial tool. (03#T1)
14. **Latency metrics measure what users feel:** time to first visible text, split into direct answers and answers after tool use, excluding time spent waiting on the user.
15. **Pricing is data:** regional multipliers, a price tier for long prompts, and organisation overrides accepted only from admin-controlled settings.

## 2. Context, caching and compaction

16. **Per-session facts moved out of the system prompt and the start of the conversation, and into the transcript as change-only updates.** These are working directory, model, instruction files and git state. Editing an instruction file no longer invalidates the whole cached conversation. *Treat context as an append-only log.* (02#T1, 02#T2)
17. **The prompt a session was started with is recorded and replayed on resume.** Differences from the current build reach the model as a short delta message rather than a rewritten prefix. Rendering choices, such as reminder placement and how tool-list changes are announced, are fixed when the session starts and stored with it. *A decision that flips mid-session changes the bytes of old turns and breaks the cache.*
18. **Compaction now re-states standing context after the summary.** Facts that are logged once would otherwise be lost. *Moving from "rebuilt every call" to "logged once" makes re-announcement part of the compaction contract.*
19. **Compaction is timed to the prompt cache.** Large idle sessions are summarized at about 90% of the cache lifetime, while the cached prefix is still cheap to read. The summary is skipped if anything changed in the meantime. After a long idle gap, when the cache is already cold, old tool results are cleared on the server instead of being re-sent. *Schedule maintenance against your cache lifetime, not just token counts.* (05#T5)
20. **Cache lifetime is set separately for the main conversation and for subagents and helpers,** with clear precedence (environment, settings, agent definition, default) and a reason recorded for each request. *The main thread and short-lived helpers have opposite cache economics.* (01#T9)
21. **Every request audits its uncached tail:** tokens sent after the last cache marker are attributed to a reason. *Caching regressions are silent cost leaks; make them visible.*
22. **There are more escape hatches before a context overflow error:** compacting with a larger-window model, compacting after a "too long" rejection (with checks for when it can't help), and handling overflow inside auxiliary classifier calls.
23. **Summaries are more honest about what they missed.** They say when the earliest part was dropped. They warn that the kept recent messages were never shown to the summarizer. They mark third-party content as data, not instructions. (05#T11)
24. **A hook can take over compaction entirely,** supplying the replacement messages while boundaries, accounting and post-compaction hooks still run.
25. **Memory writes get immediate feedback.** After a write the file is checked against the limits recall actually applies, and the model is asked to split or rewrite it if it's too big. Background memory extraction stops writing once a newer user turn starts. Turning memory off mid-session appends a retraction rather than rewriting earlier turns. Several older memory modes were removed in favour of a single index-plus-recall design. (02#T8)
26. **AGENTS.md is loaded.** Projects without a native instruction file load the cross-tool agent-instructions file by default, using the same precedence (see 02 R21a). An importer maps other tools' rule files.
27. **A dormant staged-context-folding mechanism was removed,** including its storage formats and resume support. *Experimental context strategies leave persistence formats behind, so version your transcript entry types.*

## 3. Tools, safety and hooks

28. **Deleting files whose paths are only known at run time now needs a human.** That covers paths from a variable that might be empty, a variable set earlier in the same command, or the output of a command. The denial tells the model how to rewrite the command safely. *A denial message that contains the fix stops retry loops.* (04#T11)
29. **Inline shell scripts (`sh -c …`) are analysed recursively**, with a depth limit and a cost budget. When the budget runs out the analysis fails closed and says so explicitly. *Analysis that stops at the outer command is easy to bypass.*
30. **Each shell dialect has its own rules for catastrophic delete targets,** and a shell launched from inside another gets its own coverage.
31. **Approval prompts for dangerous actions expire.** An unanswered prompt becomes a denial after a few minutes, and after several in one session further ones are denied without being shown. The decline option is pre-selected and there is no single-key approve. A small-model "explain this command" helper was removed. *Deterministic friction beat generated explanations.*
32. **The hazard registry doubled.** Each entry now declares whether bypass mode can skip it, whether the classifier may judge it, whether auto mode must hard-deny it, and whether only a person may approve it. *Keep policy in data.* (04#T3)
33. **The auto-mode judge gets harness-gathered facts:** repository state, branch versus default branch, whether the tree is clean or dirty, repository visibility, and the working directory, all size-capped. (04#T6)
34. **Allows that don't come from the judge got weaker.** An extension's "allow" still goes to the judge in auto mode, and can't override gates that require a person. Some tools are judge-only, so saved allow rules never apply to them. *Keep exactly one authority for autonomous approval.* (04#T2)
35. **A read-confinement setting** covers file tools in every mode, shell commands with paths computed at run time (which ask a person), and the OS sandbox. *Confidentiality needs its own fence; a write-focused model leaves secrets readable.*
36. **Broken security config fails closed** in a restricted session mode: if settings or guard hooks fail to load, tool calls are blocked.
37. **Hooks can fail closed per hook**, covering crashes, timeouts and bad output. Programmable plugin hook modules are checked statically before loading, and new events fire before and after a model switch. (04#T9)
38. **Slow calls can leave the turn without being killed.** Each tool declares whether it backgrounds itself, can be detached by the harness, or must never move. A detached call gets an honest placeholder result, and the real result arrives later as a notification. *The user should never wait on a slow read-only call before the agent can reply.*
39. **Checking background work no longer blocks.** A blocking "wait for output" tool was replaced by a status check, plus a separate tool that waits for harness events, each of which records where it came from.
40. **Shell commands report the files they changed**, by comparing repository state before and after. (03#T4)
41. **Sandbox grants resist symlink swaps** made after the policy was built, and high-risk callers can require full confinement instead of silently getting less.

## 4. Agents, MCP and hosting

42. **The CLI is code-split.** A tiny entry point loads each subsystem only when needed, and large reference documents became compressed data files read on demand. *Startup cost should depend on what a session uses, not on how many features ship.*
43. **Subagents report back through a dedicated hand-back tool.** They call it once, which ends their run. The report gets a safety review and is marked delivered, delivered with a warning, or withheld. Text added by the harness is kept separate from text the agent wrote. *The last message is a fragile, spoofable result channel.* (06#T2)
44. **A subagent that hits its turn limit is labelled truncated**, and the parent is told how to let it continue. *Hitting a budget is an outcome, not a success.*
45. **The subagent stall timeout comes from the stream timeout plus a grace period**, and is deferred while tools are running. *Stacked watchdogs must let the inner one recover first.* (06#T12)
46. **Agents that are stopping refuse new work:** subagents, workflows, skills, monitors and messages.
47. **Operators can lock a subagent's model by removing the option from the tool schema.** A spawn-time effort option exists but is meant only for explicit requests. Forks always inherit the parent's model and effort. *Taking an option out of the schema is more reliable than telling the model not to use it.*
48. **Background results go back to the agent that started the work.** Large results are saved to a file and referenced. A newer notification replaces an older one for the same task. (06#T10)
49. **Spawns rewritten by plugins are checked again** against the permission rules, and impossible combinations are refused. (06#T6)
50. **MCP startup no longer always blocks the first headless turn.** When tool search is available, tools are discovered later anyway, and hosts can set the startup wait, including to zero. (07#T6)
51. **MCP descriptions have two length budgets:** a short one when shown up front, a larger one when the model loads the tool on demand.
52. **The MCP discovery cache is sturdier.** It is encrypted at rest, expires sooner when stale, remembers the negotiated protocol, and is dropped when the signed-in identity changes. Failed servers are retried next turn. Legacy transport fallback and protocol negotiation results are remembered.
53. **SDK hosts send their in-process tool lists in the opening handshake**, which removes a startup round trip per server. Hosts can also supply plugins up front and subscribe to session-state changes. (07#T10)
54. **When a host reconnects before answering a prompt, the outcome is reported as "not executed, retry", not as a denial.** *Telling "no answer" apart from "no" keeps the model from learning the wrong lesson.*
55. **Hosts can declare worktree fences.** The fence is validated: it must contain the session folder and must not be overly broad. File tools then refuse writes outside the worktree.
56. **Timers are session state.** Pending wakeups survive compaction, resume, background hand-off and restart, and don't fire again after a fork. One-shot runs wait for them instead of exiting. (06#T9)

---

## Unchanged (checked in both builds)
Core retry constants and backoff, heartbeat and stall timers, the output-limit recovery cap of 3, terminal reasons, the core Edit, Read and Write mechanics, the core OS sandbox, the subagent depth and concurrency caps, the shell auto-background threshold, and the defaults for team teardown and nonblocking MCP connect.

## Method and limits
- Static comparison only: string-anchor counts in both builds, plus reading the surrounding code. Nothing was executed.
- A naive comparison of string literals overstates what's new. Many settings are read in a way that doesn't show up as a quoted string, so every claim here was checked by searching both builds directly.
- Raw evidence (offsets and identifiers) is kept privately and isn't published, per [`CLEANROOM.md`](../CLEANROOM.md).
- `cleanroom/harnesslab` was built from spec revision 1, so it doesn't yet include R21a (AGENTS.md loading) or the changes above.
