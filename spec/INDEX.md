# Coding-agent harness: functional specification index

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

## What this specification set is

- **Purpose:** a buildable description of a coding-agent harness — an agent engine plus the hosts (terminal, desktop GUI, IDE, SDK) that embed it — expressed as behaviour, data shapes, constants, algorithms and rationale.
- **Subject:** behaviour observed in a Claude Code CLI build (v2.1.295) and the Claude desktop app (v2.31226.1) on macOS arm64.
- **Status of claims:** each requirement carries "Confidence: observed" (seen directly in shipped artifacts) or "Confidence: inferred".
- **What it is not:** it contains no original code, prompt wording, internal identifiers or citations. Prompt fragments are described by purpose and required content only; implementers write their own text.

Each spec file follows the same outline: 1 Purpose & scope · 2 Concepts & data model · 3 Behavioural requirements · 4 Algorithms · 5 Constants & knobs · 6 Rationale · 7 Harness design takeaways (T1…Tn) · 8 Uncertainties.

## File map

| # | Spec file | Covers |
|---|---|---|
| 00 | [desktop-architecture](00-desktop-architecture.md) | GUI process model, IPC validation, utility processes, preview panes, terminal, VM sidecars, runtime lock-down |
| 01 | [agent-loop-and-api](01-agent-loop-and-api.md) | turn-loop state machine, streaming tool execution, retry policy, prompt caching, token accounting |
| 02 | [system-prompt-and-context](02-system-prompt-and-context.md) | prompt sections and cache boundary, typed context attachments, instruction-file hierarchy, memory |
| 03 | [tools](03-tools.md) | tool interface and inventory, concurrency, read ledger, edit matching, shell execution, deferred tool loading |
| 04 | [permissions-sandbox-hooks](04-permissions-sandbox-hooks.md) | rule engine, shell parsing, auto-approval classifier, OS sandbox and egress proxy, hook events |
| 05 | [context-management-and-sessions](05-context-management-and-sessions.md) | context-pressure ladder, summarisation, transcript DAG, file checkpoints and rewind |
| 06 | [multi-agent-and-background](06-multi-agent-and-background.md) | subagents, forks, deterministic workflows, notification queue, monitors, scheduled wake-ups, worktrees |
| 07 | [extensibility-sdk-mcp](07-extensibility-sdk-mcp.md) | skills, plugins, commands, MCP client, status line, host control protocol |
| 08 | [desktop-coding-features](08-desktop-coding-features.md) | GUI host around the engine: start options, permission broker with nonces, host hooks, rewind, pre-warming, CI auto-fix, prompt-fragment contracts |
| 09 | [computer-use](09-computer-use.md) | computer-use tool surface, grants and tiers, safety gates, coordinates and token budget, dispatch pipeline |
| 10 | [computer-use-native](10-computer-use-native.md) | native capture, input injection, AX-first dispatch, OS permissions, indicators, supervised helper |

## Architecture overview

```
 ╔══════════════════════════════ HOST (desktop · IDE · web · SDK) ══════════════════════════════╗
 ║  GUI surfaces ── each one = an in-process MCP tool + a short prompt contract                 ║
 ║  permission broker: canonicalise → show card → single-use consent nonce → verify in tool     ║
 ║  durable host state: sessions · plans · rewind points · worktree pool · CI watchers · notes  ║
 ║  computer-use server: grants/tiers/locks ─► native add-ons ─► supervised raw-event helper    ║
 ╚═══════════════════════════════════════╤══════════════════════════════════════════════════════╝
                                         │  newline-delimited JSON events + typed control RPC
 ╔═══════════════════════════════════════╧══════════ ENGINE (agent CLI) ════════════════════════╗
 ║  PROMPT    [static sections | cache boundary | per-org sections]  + context as messages      ║
 ║  LOOP      generator: request ─► stream ─► start tools mid-stream ─► results ─► next round   ║
 ║            named transitions: reactive compaction · output-limit continuation · fallback     ║
 ║  GATE      hooks ─► deny/ask/allow rules ─► mode ─► hazard table ─► LLM judge ─► OS sandbox  ║
 ║  RESULTS   size-budgeted; oversized output spilled to disk with a short preview              ║
 ║  CONTEXT   server clearing ─► lossless client clearing ─► precomputed summary ─► compaction  ║
 ║  ASYNC     subagents · background shells · monitors · timers ─► one notification queue      ║
 ║  STORAGE   append-only transcript (uuid / parent-uuid DAG) + per-prompt file checkpoints     ║
 ╚══════════════════════════════════════════════════════════════════════════════════════════════╝
```

## Top patterns worth copying

Stable IDs `P01`–`P28` (new patterns are appended, never renumbered). Per-file takeaways are cited as `NN#Tk` (takeaway *k* in spec file *NN*).

### Loop and API
- **P01 — Generator loop with named transitions.** A single async generator yields every event; each iteration ends in a terminal reason or an explicit "continue because …"; recovery budgets are guarded per round. (01#T1)
- **P02 — Errors are messages, held back during recovery.** API failures become assistant rows that surface only if recovery (compaction, continuation, fallback model) fails. (01#T2)
- **P03 — Retry policy as a table** keyed by error class and how much has streamed, yielding retry / non-stream retry / keep partial / fallback / fail; background traffic drops at once on overload; context overflow shrinks the output budget from the parsed error. (01#T6, 01#T7)
- **P04 — Repair tool-call pairing before every request**, with placeholder results that explain why (e.g. outcome unknown, verify before rerunning). (01#T5)

### Prompt and context
- **P05 — Cache-first prompt design.** Static sections before a boundary (global cache), per-user data after it, volatile environment in messages, one rolling breakpoint, removable reminder rows, and measurement of uncached tail tokens. (01#T8, 02#T1)
- **P06 — Context as typed attachments re-announced as diffs**, so hot reloads supersede earlier values instead of rewriting history. (02#T2)
- **P07 — Many attachment producers in parallel under ~1 s deadline, failing silently.** (02#T4)
- **P08 — Turn-counted nudges** (e.g. task-list reminder after ≥10 quiet turns, plan reminders every 5 turns) that say "ignore if not applicable". (02#T5)
- **P09 — Bound every injected blob and say what was cut**; explicit truncation prompts a tool call, silent truncation invites hallucination. (02#T7)
- **P10 — Tell the model its remaining token budget** and discourage premature wrap-up. (02#T12)

### Tools
- **P11 — Per-call concurrency from parsed input**; start execution mid-stream with an ordered queue behind serial calls. (03#T1, 03#T2, 01#T4)
- **P12 — Read ledger with content-aware staleness** (mtime + hash; side effects reflected back; redundant re-reads flagged as wasted). (03#T3, 03#T4, 03#T5)
- **P13 — Spill, don't truncate**: per-tool and per-batch caps; large outputs to disk with a short preview; memoise replacements so the cache prefix stays stable. (03#T7, 05#T4)
- **P14 — No persistent shell**: fresh shell per command sourcing a snapshot; background on timeout instead of killing. (03#T9, 03#T10)
- **P15 — Defer the long tail of tools behind a search tool**; announce names, load schemas on demand. (03#T8)
- **P16 — Error messages that name the fix**, each with a code so effectiveness can be measured. (03#T11)

### Safety
- **P17 — Typed decision reasons; layers may only tighten** (a hook "allow" is still subject to deny rules, safety checks and the classifier). (04#T1, 04#T2)
- **P18 — Deterministic checks first, LLM last**: real shell parser, per-command flag allowlists, hazard table with bypass-immune entries, then an asymmetric two-stage classifier that fails closed and hands back to a human after repeated denials. (04#T3, 04#T4, 04#T5)
- **P19 — The judge sees a curated projection** of each tool call, and consent rules state what the user must have named. (04#T6, 04#T7)
- **P20 — Sandbox the shell, proxy the network, mask secrets.** (04#T10)

### Context lifetime and async
- **P21 — Five-step pressure ladder** (server clearing → lossless client clearing → precomputed summary → cache-sharing compaction fork → reactive compaction keeping recent rounds), with circuit breakers and cache-TTL-aware scheduling. (05#T1, 05#T2, 05#T3, 05#T5, 05#T6, 05#T8)
- **P22 — Append-only uuid-DAG transcript** giving fork, rewind and sidechains; copy-on-first-touch file checkpoints; lazy, undoable rewind via resume-at-point plus fork. (05#T9, 05#T10, 08#T7)
- **P23 — Async by default with anti-fabrication prompting**; one notification queue for all async sources; results carry a continuation handle. (06#T1, 06#T2, 06#T10)
- **P24 — Cache-identical forks for context offload**, and deterministic workflow scripts with forced structured output and hard caps for large fan-out. (06#T3, 06#T4, 06#T7, 06#T8)

### Host and extensibility
- **P25 — One engine, many hosts** over a typed control protocol: host tools run in-process; every GUI surface is a tool plus prompt contract whose results steer the next step; approvals bound to the shown input by a single-use nonce; durable state lives in the host and is re-injected after compaction; skills, commands and plugins share one record type with progressive disclosure under a budget. (01#T12, 07#T1, 07#T2, 07#T8, 07#T10, 08#T1, 08#T2, 08#T3, 08#T6)

### Computer use
- **P26 — Policy inside the tool server with three separate consents** (which apps, screen takeover, OS permissions); category tiers that only go down (browsers and trading apps view-only, terminals/IDEs click-only, others full); re-check frontmost app and hit-test target at delivery time. (09#T1, 09#T2, 09#T3, 09#T4)
- **P27 — Semantic actions before pixels, fail closed.** AX actions first; raw per-process events only for allowlisted refusal reasons; the model speaks screenshot pixels and the server rescales; screenshots fit a token budget (28 px/token, ≤1568 tokens, JPEG ≈0.75) with `zoom` for detail; steer toward dedicated tools and CLIs before GUI clicking. (09#T6, 09#T7, 09#T8, 09#T13, 10#T3, 10#T4)
- **P28 — Isolate risky native code and leave no trace**: public APIs in-process, private crash-prone APIs in a supervised JSON-RPC helper; hostile-text sanitisation; own UI excluded from captures; Esc kill switch ignoring own presses plus visible indicator; restore hidden apps and clipboard at turn end. (09#T5, 09#T10, 09#T11, 09#T12, 10#T1, 10#T5, 10#T6)

## Biggest open questions (across files)

- The GUI renderer, compiled bytecode and some native add-ons were not decoded; their behaviour is inferred from callers.
- Server-side behaviour (context-management edits, capability negotiation) is inferred from client behaviour.
- Feature-flagged variants depend on remote configuration whose values are unknown.
- Nothing was observed at runtime; capturing a live host↔engine session would confirm protocol details in 07 and 08.
