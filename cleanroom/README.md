# harnesslab

A readable, runnable **reference implementation of a coding-agent harness** in
Python: the turn loop, streaming tool execution, permissions and hooks, context
assembly, compaction, transcripts, subagents, skills/MCP and a host protocol. It
is meant to be read: every module says which requirement of the specification it
implements, and the whole thing runs offline against a scripted model.

## Clean-room note

harnesslab was built **only from the functional specification in `../spec/`**
(start with `spec/INDEX.md`). The specification describes behaviour, data shapes,
constants and algorithms; it contains no original code or prompt text. No vendor
source, bundle or prompt was consulted while writing this code, and every prompt,
nudge and section text here is our own wording written from the purpose the spec
describes. Module docstrings cite requirements (`03 R17`), algorithms (`05 §4.2`)
and takeaways (`01#T4`) so you can read code and spec side by side.

* Python 3.11+, **standard library only** (an optional `anthropic` extra adds a real model).
* **Fully offline**: `ScriptedModel` replays scripted assistant turns (text, tool calls,
  API errors, max-tokens cuts, malformed JSON, thinking-only turns, callables).
* Where the spec needs native or OS code (screen capture, Electron host, sandbox
  enforcement) the code lives in `harnesslab/pseudo/` as clearly labelled,
  Protocol-based pseudocode with a simulated backend.

## Quick start

```bash
cd cleanroom
uv run --with pytest pytest -q                 # ~780 tests, offline, a few seconds
uv run python -m harnesslab.cli                # demo REPL: try `list *.py`, `read README.md`, `run ls`
uv run python examples/offline_session.py      # a scripted read -> edit -> bash session
uv run python examples/permission_playground.py
uv run python examples/compaction_demo.py
uv run python -m harnesslab.cli --ndjson       # speak the NDJSON host protocol on stdio
uv run --with anthropic python -m harnesslab.cli --model <model-id>   # real model (optional)
```

Without uv: `python3 -m venv .venv && . .venv/bin/activate && pip install pytest && pytest -q`
(scripts and the CLI need `PYTHONPATH=.` when run without uv).

The demo REPL's "model" is a tiny rule-based ScriptedModel: `read <path>` calls Read,
`run <cmd>` calls Bash, `list <glob>` calls Glob, anything else is echoed. Everything
else - loop, permission prompts (`y`/`n`/`a`), transcript under `~/.harnesslab` - is
real. `--no-persist` skips the transcript, `--permission-mode` picks a mode.

## A guided tour: one turn, end to end

Read these ten files in order; together they are one user turn.

1. **`harnesslab/session.py`** - `create_session_context` wires every subsystem into a
   `SessionContext`; `Engine.submit` runs UserPromptSubmit hooks, snapshots files for
   rewind, expires last turn's context layers and calls the loop.
2. **`harnesslab/loop/query.py`** - `run_query`, the generator turn loop: fold inputs,
   auto-compact, block check, build request, stream, recovery ladder, stop hooks, drain
   tools, max-turns, next round. Each step is a named transition (01 R1-R12).
3. **`harnesslab/context/attachments.py`** - typed per-turn context (environment, date,
   instruction files, skill listing, todo nudges, edited files...) produced in parallel
   under a 1 s deadline (02 R10-R14, R30-R32).
4. **`harnesslab/api/request.py`** + **`harnesslab/context/prompt.py`** - the request:
   memoized system-prompt sections around a cache boundary, model, thinking, effort,
   max_tokens, tool schemas (01 R36-R41, 02 R1-R9).
5. **`harnesslab/loop/normalize.py`** - attachments become `<system-reminder>` text,
   tool_use/tool_result pairing is repaired, media capped (01 R55-R57).
6. **`harnesslab/api/retry.py`** - streaming with retries, backoff, mid-stream policy,
   tombstones and model fallback (01 R14-R25).
7. **`harnesslab/tools/executor.py`** - tools start the moment their block completes;
   read-only calls run in parallel, writes are barriers, results stay in order (01 R9-R12).
8. **`harnesslab/tools/pipeline.py`** - one call: parse, coerce, schema, validate,
   permission decision, call, map, spill, PostToolUse hooks (03 R1-R8).
9. **`harnesslab/permissions/pipeline.py`** - PreToolUse hooks -> rules -> tool check and
   shell analysis -> mode -> classifier -> human, always ending in allow or deny (04 §4.1).
10. **`harnesslab/compaction/controller.py`** + **`harnesslab/transcript/store.py`** -
    context pressure ladder and the append-only uuid DAG every row is written to
    (05 §4.2, R36-R45).

Then `tests/test_e2e_offline.py` shows the same path driven by a script, including
compaction + resume, a subagent with a continuation handle, and a skill.

## Module map → spec

| Module | What it is | Spec |
|---|---|---|
| `types.py` | content blocks, `Message`, `Attachment`, loop enums & events, synthetic-result texts | 01 §2, 02 §2.3, 03 §2.4 |
| `config.py` | merged `Settings` with sources, env helpers | 04 §2.3 |
| `runtime.py` | `SessionContext`: per-session/agent state, on-disk paths | 01 §2.1, 03 §2.3, 05 §2.6 |
| `interfaces.py` | cross-package Protocols (executor, can-use-tool, hooks, attachments, compactor, ...) | 01 R2 |
| `tokens.py` | token estimate + usage-anchored context size | 01 R47-R48, 05 R1-R4 |
| `util/` | abort controllers, reminder wrapping, ids/slugs, frontmatter, JSON-schema | 01 R12, 02 R30 |
| `model/base.py`, `model/scripted.py` | `Model` protocol, stream types, `StreamAccumulator`, `ScriptedModel` | 01 R9, R14-R15 |
| `model/anthropic_adapter.py` | optional real backend | 01 R15, R19 |
| `loop/state.py`, `loop/query.py` | loop state, guards, the generator turn loop | 01 R1-R12, §4 |
| `loop/recovery.py`, `loop/nudges.py` | recovery ladder and nudge texts | 01 R26-R32 |
| `loop/normalize.py` | normalization + pairing repair before send | 01 R55-R57 |
| `loop/context_layers.py` | tool-requested temporary context (Skill grants, model, effort) | 03 R12, 07 R8 |
| `api/retry.py`, `api/caching.py`, `api/request.py`, `api/accounting.py` | retries, one rolling cache breakpoint, request assembly, cost | 01 R14-R50 |
| `context/prompt.py` | memoized sections, cache boundary, agent/fork prompts | 02 R1-R9, R35-R36; 06 R14, R26 |
| `context/attachments.py` | parallel producers, diffs, throttled nudges, skill listing | 02 R10-R14, R30-R32, R37 |
| `context/instructions.py`, `context/memory.py` | instruction files (CLAUDE.md, cross-tool AGENTS.md via the `agentsMd` setting), `@imports`, auto-memory | 02 R15-R29, R21a |
| `tools/base.py`, `tools/registry.py` | the `Tool` contract, lookup, deferral | 03 §2.1, R54-R55 |
| `tools/pipeline.py`, `tools/executor.py` | per-call pipeline, streaming scheduler | 03 R1-R14 |
| `tools/ledger.py`, `tools/edit_match.py`, `tools/output.py` | read ledger, forgiving edits, output budgets/spill | 03 R17-R33 |
| `tools/shell.py`, `tools/builtin/*` | snapshot shell; Read, Write, Edit, Glob, Grep, Bash, TodoWrite, ToolSearch | 03 R38-R57 |
| `permissions/*` | rules, hazards, shell analysis, modes, classifier, pipeline | 04 R1-R30, §4.1-§4.6 |
| `hooks/*` | events, matchers, runner, merge | 04 R31-R40, §4.7 |
| `compaction/*` | thresholds, controller, summarizer, micro-compaction, re-injection | 05 R5-R35, §4.1-§4.6 |
| `transcript/*` | JSONL rows, uuid DAG store, resume/fork, checkpoints & rewind | 05 R36-R48, §4.7-§4.9 |
| `agents/*` | tasks, notifications, definitions, launcher, Agent/SendMessage/TaskStop, workflows | 06 R1-R42 |
| `extensions/*` | skills + Skill tool, MCP naming/schemas/output caps, in-process servers | 07 R1-R27 |
| `host/protocol.py`, `host/broker.py` | NDJSON stream + control RPC; consent-nonce permission broker | 07 R30-R38; 08 §4.2 |
| `cli.py` | demo REPL and `--ndjson` host entry point | 01 R59, 07 R30 |
| `pseudo/*` | **[pseudo]** sandbox profiles, computer use, desktop host | 04 R41-R46; 09; 10; 00; 08 |

`ARCHITECTURE.md` has the layer diagram, the data flow of one turn and the list of
`ctx.extra` seams between packages.

## What is simulated (`harnesslab/pseudo/`)

* `sandbox_profiles.py` - real profile/argument *generation* (seatbelt-style profile
  text, bubblewrap argv, egress policy order) plus a simulated sandbox that makes
  allow/deny decisions; nothing is actually confined.
* `computer_use.py` - real policy (screenshot sizing, coordinate mapping, grant tiers,
  action gates, batch rules, helper budgets) over native seams that are Protocols, with
  a `SimulatedDesktop` backend.
* `desktop_host.py` - the GUI host's algorithms (environment shaping, shim rules,
  worktree argv, append freezing, CI de-dup, mention scoring) without Electron.

## Deferred (not implemented on purpose)

WebFetch / WebSearch / LSP / NotebookEdit / AskUserQuestion / plan-mode tools, Monitor,
Cron and wake-ups, agent teams, worktree isolation, plugins and marketplaces, status
line, remote control, transcript retention sweep, MCP stdio/http transports and OAuth
(only in-process `sdk` servers are real), managed/synced/bundled skill catalogs. Each
module docstring lists its own deferred items. See `WORKPLAN.md` for the history of
how the five work packages were built and integrated.
