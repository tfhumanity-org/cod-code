# Harness evaluation rubric

This rubric scores a target harness on 15 dimensions. Each dimension lists what to look for in the target, the reference evidence to compare against, and a 0–3 maturity scale.

**Reference IDs.** `NN#Tk` means takeaway *k* in `spec/NN-*.md` (the "Harness design takeaways" section). `P07` means pattern 7 of the top patterns (`P01`–`P28`) in `spec/INDEX.md`.

**Scoring.**

| Score | Meaning |
|---|---|
| 0 | Absent |
| 1 | Ad-hoc: exists, but is implicit, untyped, or untested |
| 2 | Deliberate: designed and covered by some tests, with known gaps vs. the reference |
| 3 | On par with or beyond the reference pattern |
| N/A | The dimension doesn't apply to this product. Say why |

**Applicability.** Not every harness needs every dimension. A single-shot CLI agent may score most of D11 as N/A. Judge against what the target is *trying* to be, and record that intent in the scorecard header.

---

### D1 — Agent loop and recovery · ref `01`, P1, P2, P4
- **Look for:** the main turn loop (recursion vs. iteration), stop conditions, max-turn and budget limits, the abort/interrupt path, and what happens on max_tokens, prompt-too-long, a malformed tool call, or a thinking-only reply.
- **Signals of 3:** explicit terminal-reason and transition enums, and per-round recovery guards with caps. Errors are held back while recovery is attempted. Orphan tool_use/tool_result pairs are repaired before each request, and synthetic results explain why.
- **Red flags:** unbounded retry loops, swallowed errors, orphan tool_use blocks that crash the API call, and recursion depth growing with turns.

### D2 — API layer, retries, and cost · ref `01`, P3, P5
- **Look for:** the retry/backoff code, error classification, fallback models, streaming failure handling, the prompt-cache breakpoint strategy, cache TTL choice, token counting, and cost tracking.
- **Signals of 3:** the retry policy is a table keyed on (error class × stream progress). Background traffic drops on overload. Partial output is kept once it has streamed. Breakpoints are placed deliberately and stay stable across turns, and uncached tail tokens are measured.
- **Red flags:** cache breakpoints on volatile content, timestamps or cwd inside the system prompt, a fixed sleep for retries, and no `retry-after` handling.

### D3 — Prompt and context assembly · ref `02`, P5–P10
- **Look for:** how the system prompt is built (sections, ordering, model-specific variants), where environment, git, and date info go, how reminders are injected, and how changes such as a new cwd or reloaded config reach the model.
- **Signals of 3:** a static/dynamic split with a cache boundary. Volatile context lives in messages and is re-sent only as diffs. Attachment producers run in parallel under a deadline. Nudges are throttled by turn count. Every injected blob is bounded, and the model is told what was cut. The token budget is visible to the model.
- **Red flags:** one giant f-string prompt, the whole prompt rebuilt every turn, unbounded file or command output pasted into context, and silent truncation.

### D4 — Instructions and memory · ref `02`
- **Look for:** project instruction files (CLAUDE.md, AGENTS.md, rules), their discovery order and precedence, lazy or path-scoped loading, any persistent memory store, recall strategy, and size caps.
- **Signals of 3:** each file is labelled with where it came from and loaded in a defined precedence. Path-scoped rules load lazily. Memory is an index plus atomic files with a cheap recall selector and a byte budget. Memory writes are sanitized against injected harness tags.

### D5 — Tool design and execution · ref `03`, P11, P14–P16
- **Look for:** the tool interface or type, schema validation, the description/prompt per tool, the parallel execution policy, and Bash/shell execution (persistent vs. fresh, cwd, timeouts, background processes).
- **Signals of 3:** a single tool builder with safe defaults. Concurrency is decided per call from the parsed input, execution starts while the response is still streaming, and results are returned in order. Errors name the fix and carry a code. Input coercion accepts field aliases. Shell state is snapshotted rather than kept in a long-lived shell. Commands that time out are backgrounded instead of killed. Rarely used tools are deferred behind a search tool.
- **Red flags:** all tools run serially, or all in parallel with no write safety. Errors are bare exceptions. Tool descriptions are copy-pasted one-liners.

### D6 — Output budgeting and file-state integrity · ref `03`, `05`, P12, P13
- **Look for:** limits on tool output size, the truncation strategy, a read-before-edit ledger, staleness detection, edit matching rules, and detection of side effects such as formatters changing files.
- **Signals of 3:** size caps per tool and per batch. Large outputs are spilled to disk with a preview, and the replacement is memoized. A ledger of mtime plus content hash, with edits that still apply cleanly allowed through when only the mtime changed. Forgiving matching (quotes, escapes) that writes back in the file's own style. Re-reads of unchanged files are flagged as wasted calls.

### D7 — Permissions and policy · ref `04`, P17–P19
- **Look for:** permission modes, the rule syntax and its precedence, how shell commands are parsed for safety, any LLM-based judge, and how denials are reported to the model.
- **Signals of 3:** a typed `decisionReason`. Layers can only tighten, never widen. A real shell parser plus flag allowlists runs before any LLM. The hazard list is data, not code, with flags that survive bypass mode. Any classifier is asymmetric and fails closed, and its input is a curated projection of the request. Denial messages say "NOT performed". Broad allow rules are stripped when autonomy increases.
- **Red flags:** regex or substring command blocklists, allow-by-default, and a model-written "user approved" being trusted.

### D8 — Sandbox, network, and hooks · ref `04`, P20
- **Look for:** OS-level sandboxing, network egress control, secret handling, and lifecycle hooks (events, I/O contract, exit-code semantics, timeouts).
- **Signals of 3:** a sandbox profile generated per command. Shell rc files and `.git/hooks` are write-protected. Egress goes through a deny-then-allow proxy, and real credentials are swapped in only at that proxy, so the sandbox never holds them. Hooks have a small typed contract (exit 2 means block, and stderr goes back to the model), with `if:` filters and LLM-verifier hook types.

### D9 — Context lifetime and compaction · ref `05`, P21
- **Look for:** what happens as context fills: thresholds, the summarization prompt, what is re-injected afterwards, circuit breakers, and manual compaction.
- **Signals of 3:** a layered ladder that goes from cheapest and reversible to most lossy. Summarization runs as a fork that reuses the prompt cache. The summarizer is told what will survive. Clearing is lossless because content is persisted to disk. A thrash breaker exists. Reactive compaction keeps recent rounds verbatim, and user constraints are preserved verbatim.
- **Red flags:** truncating the oldest messages blindly, compacting with no re-injection, and no loop protection.

### D10 — Persistence, resume, and rewind · ref `05`, `08`, P22
- **Look for:** the transcript format, resume and fork support, file checkpoints, and undo.
- **Signals of 3:** an append-only event log whose entries link by uuid and parent uuid, so the history forms a tree. Metadata uses last-write-wins rows. Files are checkpointed copy-on-first-touch at each prompt. Rewind is lazy and can be undone, and the model is told when the code and the conversation have diverged.

### D11 — Multi-agent and async work · ref `06`, P23, P24
- **Look for:** subagents, how their tools and prompt are built, how results come back, background execution, notifications, scheduling, and worktree isolation.
- **Signals of 3:**
  - Async by default, with a prompt that tells the model not to invent pending results.
  - Results carry a handle the model can use to continue the agent.
  - Forks reuse the parent's cache.
  - Tool pools are filtered by role, and a filter that fails is treated as a refusal.
  - Hard caps (depth, concurrency, budget) whose errors tell the model what to do instead.
  - One notification queue, with flow control at each source.
  - Deterministic orchestration scripts for large fan-outs.

### D12 — Extensibility · ref `07`
- **Look for:** skills/commands/plugins, MCP or tool-server support, how extensions are listed to the model, and limits on their output.
- **Signals of 3:** one record type for commands from every source. Skills are listed under a character budget with progressive disclosure. Skills can carry context overrides (allowed tools, model, effort). MCP connects without blocking, output is capped and paginated, and tool names are sanitized. Plugin manifests are fenced and binaries are pinned.

### D13 — Host protocol and UI integration · ref `00`, `07`, `08`, P25
- **Look for:** how UIs (CLI, IDE, web, desktop) drive the engine, the event and control protocol, the permission prompt contract, and where durable UI state lives.
- **Signals of 3:** one engine with a typed, versioned control channel. Host tools run in-process. Every UI surface is a tool plus a prompt contract, and tool results tell the model what to do next. Approvals are bound to the input that was shown via a nonce. The host owns durable state and re-injects it after compaction. Capabilities fail closed when a host doesn't declare them.

### D14 — Computer and browser use · ref `00`, `09`, `10`, P26–P28
- **Look for:** screen capture, input injection, browser automation (CDP / accessibility tree), app/site access grants, defenses against prompt injection from screen content, and a visible indicator that the agent is acting.
- **Signals of 3:**
  - Access is granted per app or per site, in tiers.
  - The accessibility tree is preferred over pixels.
  - The agent's own windows are excluded from capture.
  - Input is aimed at a specific process rather than posted globally where possible.
  - User input interrupts the agent.
  - Native code is split out so it runs with the right OS permission identity.

### D15 — Observability and evaluation · cross-cutting
- **Look for:** telemetry or event names, error codes on tool failures, turn and terminal-reason logging, cost and cache dashboards, a statusline, and evals or regression tests for prompts and tools.
- **Signals of 3:** every recovery path and terminal reason is logged with a stable name. Prompt wording is gated behind flags and A/B-testable. A tool-error `errorCode` lets you measure which fixes work. There is a replayable transcript-based eval.
