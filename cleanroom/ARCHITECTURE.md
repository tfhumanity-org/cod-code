# harnesslab architecture

## 1. The big picture

```
            front-ends (one engine, many hosts - 01#T12, 07#T8)
   cli.py (REPL)        host/protocol.py (NDJSON + control RPC)       tests
        \                         |                                   /
         \________________  session.Engine  _________________________/
                                  |  builds SessionContext (runtime.py)
                                  v
 ┌──────────────────────── loop/query.run_query (generator) ──────────────────────────┐
 │ 1 fold inputs   agents.notifications ─┐  pending user input ─┐  context.attachments  │
 │ 2 autocompact   compaction.controller (Compactor)                                    │
 │ 3 block check   tokens.context_tokens + compaction.thresholds                         │
 │ 4 build request api.request ─ context.prompt (system blocks, cache boundary)          │
 │                              ─ loop.normalize (attachments→reminders, pairing repair)  │
 │                              ─ api.caching (one rolling breakpoint)                   │
 │ 5 yield StreamRequestStart                                                            │
 │ 6 stream        api.retry.call_with_retries ─ model.* (Scripted / Anthropic)          │
 │                 StreamAccumulator → completed ToolUseBlock → tools.executor.add()     │
 │                     executor → tools.pipeline.run_tool_use per call:                  │
 │                        coerce → schema → validate_input                               │
 │                        → ctx.can_use_tool (permissions.pipeline: PreToolUse hooks →   │
 │                          rules → tool check/shell analysis → mode → classifier → human)│
 │                        → Tool.call → map_result → tools.output (spill) → PostToolUse  │
 │ 7 recovery      loop.recovery (reactive compact / continuation nudges / retries)      │
 │ 8 stop hooks    hooks.runner (Stop)                                                   │
 │ 9 drain         executor.drain_all → batch budget → context layers → PostToolBatch    │
 │                 hooks → tool / permission end-turn requests → mid-turn input          │
 │10 max turns  →  11 state.next(NEXT_TURN)                                              │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        |  every row appended to transcript.store (uuid DAG)   file edits → transcript.checkpoints
        v
   yields LoopEvent (types.py) ... TurnEndEvent(TerminalReason)
```

Layering (lower layers never import higher ones at module import time):

```
 L0  types, config, util/*, tokens                      (FINAL, no harnesslab deps beyond L0)
 L1  model/base, model/scripted, permissions/types, hooks/types, transcript/rows,
     agents/tasks, agents/notifications                 (FINAL)
 L2  runtime (SessionContext), tools/base, interfaces, loop/state   (FINAL)
 L3  feature modules (WP1..WP5) - depend on L0-L2 at import time;
     cross-feature calls go through interfaces.* or LOCAL imports inside functions.
 L4  session.py, cli.py, host/protocol.py               (wiring)
```

## 2. Data flow of one turn

1. **Submit.** `Engine.submit(prompt)` expires the previous turn's context layers
   (`loop.context_layers.expire_layers`), runs `UserPromptSubmit` hooks (a block erases
   the prompt, 04 R33), asks `FileHistory.snapshot(message_id)` for rewind
   (05 R46), appends `Message.user(prompt)` and calls `run_query(items, ctx, params, deps)`.
2. **Fold inputs (01 R2.1).** Notifications queued by background tasks
   (`ctx.notifications.drain()`), mid-turn user input (01 R54) and attachments from
   `AttachmentPipeline.collect` (all producers in parallel, 1 s shared deadline,
   failures silent - 02 R31). Attachments are *typed records* in the item list; they
   only become `<system-reminder>` text when normalized for the request (renderers
   registered by `context.attachments` wrap their own text; `normalize.as_reminder`
   wraps only text that is not already wrapped, so every reminder has one envelope).
3. **Context pressure (01 R2.2-3).** `Compactor.maybe_autocompact` may return a
   `CompactResult` (boundary, summary, kept items, re-injected attachments); every new
   row of it is appended to the transcript (also for reactive compaction).
   `tokens.context_tokens` + `compaction.thresholds.levels` decide `blocked`.
4. **Request (01 R2.4).** The loop first publishes `conversation_items` /
   `current_items`, then `build_model_request` assembles: system blocks from
   `SystemPromptBuilder` (shared sections → boundary → session sections),
   `to_api_messages` (attachments rendered, pairing repaired, media capped), tool
   schemas (deferred ones hidden unless discovered), one cache breakpoint, model /
   thinking / effort / max_tokens. Afterwards the loop publishes `cache_safe_params`,
   `prefix_tokens` and (main thread) `rendered_system_prompt` (see §8).
5. **Stream (01 R2.5-6).** `call_with_retries` yields raw stream events (and
   retry/tombstone/model-switch loop events). `StreamAccumulator.feed` returns blocks as
   they complete; each `ToolUseBlock` goes straight to `executor.add`. Between events
   the loop yields `executor.drain_completed()` (results in original order; an unsafe
   unfinished call is a barrier). Retries that abandon an attempt tombstone partial rows
   and `executor.discard(STREAMING_FALLBACK)`.
6. **Recovery (01 R2.7).** If the final row is an API error or a cut, the ladder
   returns `Continue(reason, extra)` (state replaced, guards kept) or `Terminal`.
7. **Stop hooks (01 R2.8)** when no tool was used; a blocking hook continues with
   `STOP_HOOK_BLOCKING` up to the cap (8).
8. **Drain (01 R2.9).** `executor.drain_all()`; `tools.output.enforce_batch_budget`
   over the whole batch *before* the rows are recorded (transcript and model see the
   same blocks); released context layers applied; `PostToolBatch` hooks; a tool's
   `end_turn` or the permission layer's `end_turn_request` ends the turn; mid-turn input
   folded.
9. **Max turns, next turn (01 R2.10-11).** `state.next(NEXT_TURN, append=...)` resets
   guards. The generator finally yields `TurnEndEvent(reason)` and returns the reason.

Every item produced is appended to `ctx.transcript` (if set) with `parent_uuid`
assigned per 05 R36 (tool results parent to their assistant row).

## 3. Key types (all final)

| Type | Module | Notes |
|---|---|---|
| `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`, `ImageBlock`, `DocumentBlock`, `ToolReferenceBlock` | `types` | `to_api()` / `block_from_api()`; `ToolUseBlock.parse_error` for malformed JSON |
| `Message` | `types` | role + blocks + transcript flags (`is_meta`, `is_api_error`, `api_error`, `usage`, `message_id`, `is_compact_summary`, `subtype`, `extra`) |
| `Attachment`, `ATTACHMENT_RENDERERS`, `render_attachment_text` | `types` | typed volatile context; renderers registered by WP4 |
| `ConversationItem = Message | Attachment` | `types` | the loop's item list |
| `TerminalReason`, `TransitionReason`, `SyntheticReason`, `SYNTHETIC_RESULT_TEXT` | `types` | 01 §2.2, R52 |
| `LoopEvent` union (`StreamRequestStart`, `MessageEvent`, `AttachmentEvent`, `TombstoneEvent`, `SystemNoticeEvent`, `RetryStatusEvent`, `ToolProgressEvent`, `TurnEndEvent`, `RawStreamEvent`) | `types` | everything the generator yields |
| `Model`, `ModelRequest`, `SystemBlock`, stream events, `APIError(kind, status, headers, ...)`, `StreamAccumulator` | `model.base` | |
| `ScriptedModel`, `ScriptedTurn`, `say/turn/call/fail` | `model.scripted` | the offline fake |
| `SessionContext` (+ `child()`) | `runtime` | per-session / per-agent state bag |
| `Tool`, `ToolCallContext`, `ToolOutput`, `ValidationResult`, `ContextLayer`, `ToolErrorCode` | `tools.base` | 03 §2.1 contract |
| `PermissionMode`, `Rule`, `Decision` (+ typed reasons), `PermissionContext`, `PermissionUpdate`, `ClassifierVerdict`, `PermissionPromptRequest`, `PermissionAnswer` | `permissions.types` | 04 §2 |
| `HookEvent`, `HookSpec`, `HookMatcherGroup`, `HookExecResult`, `AggregatedHookResult` | `hooks.types` | 04 §2.6-§2.9 |
| `LoopState`, `Guards`, `QueryParams`, `QueryDeps`, `Continue`, `Terminal` | `loop.state` | 01 §2.1 |
| `ToolExecutor`, `CanUseTool`, `PermissionPrompter`, `HookRunner`, `AttachmentSource`, `TurnInfo`, `SystemPromptBuilder`, `Compactor`, `CompactResult`, `Classifier`, `SubagentRunner` | `interfaces` | cross-WP seams |
| row dicts, `CompactMetadata`, `FileBackup`, `FileSnapshot`, `item_to_row` / `row_to_item` | `transcript.rows` | 05 §2.3-§2.5 |
| `TaskRegistry`, `BackgroundTask`, `NotificationQueue` | `agents.tasks`, `agents.notifications` | 06 §2.1, §2.4 |

## 4. Module boundaries

The code was written by five parallel work packages (see `WORKPLAN.md` for the history)
and then integrated; the boundaries below are still how the code is organised.

* **Shared core** (originally architect-owned "FINAL" modules, covered by `tests/test_smoke.py`):
  `types`, `config`, `runtime`, `interfaces`, `tokens`, `util/*`, `model/base`,
  `model/scripted`, `loop/state`, `tools/base`, `permissions/types`, `hooks/types`,
  `transcript/rows`, `agents/tasks`, `agents/notifications`, all `__init__.py`,
  `tests/conftest.py`, `tests/test_smoke.py`. Work packages must not change their
  signatures; new shared state travels through `SessionContext.extra` (§8).
* **Feature modules** each belong to one subsystem package.
* **Import rule.** A feature module may import, at module top level, only FINAL modules
  and modules of its *own* WP. Calls into another WP's module happen through an
  `interfaces` Protocol held on `SessionContext` / `QueryDeps`, or via an import
  *inside the function body* (so `import harnesslab.x` never fails while siblings are
  still stubs). `loop.query.default_deps` and `session.create_session_context` are the
  only places that wire everything together.
* **Tests.** `tests/test_wpN_*.py` test one subsystem with fakes for the others;
  `tests/test_integration_seams.py` pins each cross-package seam;
  `tests/test_e2e_offline.py` drives the real `Engine` with a `ScriptedModel`.

## 5. Concurrency and I/O model (design choice)

The spec describes async generators. For readability this implementation uses a
**synchronous generator** for the loop and **threads** (`concurrent.futures`) where
the spec requires parallelism: tool batches (01 R10), attachment producers under a
deadline (02 R31), hooks for one event (04 R34), background shells / agents (06).
Abort is cooperative via `util.abort` controllers (turn → per-tool children).

## 6. On-disk layout (05 §2.6; root = `SessionContext.config_dir`)

```
<config>/projects/<slug(cwd)>/<session>.jsonl                      transcript
<config>/projects/<slug>/<session>/subagents/agent-<id>.jsonl      subagent transcripts
<config>/projects/<slug>/<session>/tool-results/<tool_use_id>.txt  spilled outputs (03 R24)
<config>/projects/<slug>/<session>/tasks/<task_id>                 background output (03 R41)
<config>/projects/<slug>/memory/MEMORY.md                          auto-memory (02 R22)
<config>/file-history/<session>/<hex16>@v<n>                       checkpoints (05 §2.5)
```

## 7. Pseudo modules

`harnesslab/pseudo/` holds what cannot be real stdlib Python: macOS capture/input/AX
and the raw-event helper (`computer_use`), the Electron host (`desktop_host`) and OS
sandbox enforcement (`sandbox_profiles`). Each exposes Protocols for the native seams,
implements the pure policy (sizing, coordinate mapping, gates, timers, profile text)
for real, and provides a simulated backend for tests.

## 8. Cross-package seams (`SessionContext.extra`)

Packages that may not import each other at module level share state through
`ctx.extra`. The keys that matter for following a turn:

| Key | Written by | Read by | Spec |
|---|---|---|---|
| `conversation_items`, `current_items` | `loop.query` (every round) | auto-mode classifier projection, Stop prompt hooks, `AgentTool` forks | 04 §4.5, 06 §4.3 |
| `cache_safe_params`, `prefix_tokens` | `loop.query` (after building a request) | compaction summarizer (warm fork), prefix-too-large breaker | 05 R12, R17 |
| `rendered_system_prompt` | `loop.query` (main thread) | launcher copies it into forks; `SystemPromptBuilder` returns it verbatim for a fork | 06 R26 |
| `agent_system_prompt`, `omit_claude_md` | `agents.launcher` | `SystemPromptBuilder`, `instructions_producer` | 06 R14-R15 |
| `end_turn_request` | permission modes (auto-mode unavailable / denial limit) | `loop.query` after a tool batch | 04 R22-R23 |
| `pre_tool_hook_context` | `permissions.pipeline` (PreToolUse runs there only) | `tools.pipeline` appends it to the result | 04 §4.1 |
| `permission_outcomes` | `permissions.pipeline` | classifier projection | 04 §4.5 |
| `hook_env_files` | `hooks.runner` (`CLAUDE_ENV_FILE`) | `tools.shell` sources them before each command | 04 R38 |
| `bash_command_clamp` | `agents.workflow` (per workflow agent) | `permissions.pipeline` denies uncovered shell calls | 06 R37 |
| `subagent_launcher` | `agents.agent_tool` | Agent / SendMessage / TaskStop / Skill (fork) | 06 §2.2 |
| `active_layers`, `layer_model`, `turn_effort` | `loop.context_layers` | `Engine.submit` (expiry), `loop.query`, `api.request` | 03 R12, 07 R8 |
| `loop_items` | `loop.query` (turn end) | `session.Engine` keeps history after compaction rewrites | - |

Other conventions fixed during integration:

* **Denials** carry the 04 R30 guidance exactly once: `permissions.pipeline` appends it;
  `tools.pipeline.denial_text` adds its own copy only when it is missing.
* **Read-only tools** (`is_read_only` or the inferred allowlist) inside the working
  directories are allowed by the rule pipeline in every mode (04#T4); outside, they ask.
* **Hook `defer`** is `Decision("deny", HookReason(name, "defer"))`; `tools.pipeline`
  turns it into a `tool_deferred` turn end only when headless and alone in its batch
  (03 R7), otherwise it stands as a deny.
* **Notifications**: producers that notify themselves tag `task.meta["notified"]`; the
  session's fallback `on_finish` listener notifies everything else once (killed tasks
  excepted - TaskStop already answered).
* **Tool pool**: `tools.builtin.default_tools()` = core tools + Agent, SendMessage,
  TaskStop, Skill; skills are discovered at session start (user, `--add-dir`, project
  `.claude/skills` upwards, legacy `.claude/commands`) and listed by an attachment.
