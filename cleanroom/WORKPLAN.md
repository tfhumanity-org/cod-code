# WORKPLAN - filling in the `NotImplementedError`s (historical record)

> **Status: complete.** This file is kept as the record of how harnesslab was built:
> an architect laid down the shared types and seams, five work packages filled in
> the subsystems in parallel against fakes, and an integration pass wired them into
> one system. Every change request at the bottom is resolved (see the
> **Integration log**). Current structure: `README.md` and `ARCHITECTURE.md`.
> Test files named in the WP sections below exist under `tests/`; the WP1 end-to-end
> test was promoted to `tests/test_e2e_offline.py` (no skip helper).

Five independent work packages. **Every file belongs to exactly one owner.** The
architect-owned FINAL files are listed first; nobody else edits them.

## Ground rules (all WPs)

1. **Clean room.** Read only `spec/`. Write only inside `cleanroom/`. Prompt / nudge /
   section texts are *your own wording*; the spec gives required content only.
2. **Don't change FINAL signatures.** If you need something from a FINAL module, use
   `SessionContext.extra[...]` or your own module and add a line to
   *Change requests* at the bottom of this file.
3. **Import rule** (see ARCHITECTURE.md §4): top-level imports only from FINAL modules
   and your own WP's modules. Anything owned by another WP is reached through an
   `interfaces` Protocol (on `SessionContext` / `QueryDeps`) or imported *inside a
   function body*.
4. **Tests use fakes for other WPs.** Your tests must pass with every other WP still
   raising `NotImplementedError`. Use `ScriptedModel`, the `ctx` fixture in
   `tests/conftest.py`, lambdas for `can_use_tool` / `prompter`, stub compactors, etc.
   Name test files `tests/test_wpN_<topic>.py`. Shared fakes for your WP go in
   `tests/wpN_fakes.py`.
5. **Docstrings.** Keep each module docstring's spec citations accurate; add any
   design choice you make where the spec is silent ("Design choice: ...").
6. **Done =** no `NotImplementedError` left in your files, `uv run --with pytest pytest -q`
   green, every spec requirement listed for your WP either implemented + tested or
   explicitly listed under "Deferred" in your module docstring.
7. Stdlib only (the `anthropic` adapter is the only optional import, guarded).

## FINAL (architect) - do not edit

```
harnesslab/__init__.py and every subpackage __init__.py (except tools/builtin/__init__.py)
harnesslab/types.py  harnesslab/config.py  harnesslab/runtime.py  harnesslab/interfaces.py  harnesslab/tokens.py
harnesslab/util/{abort,text,ids,frontmatter,schema}.py
harnesslab/model/base.py  harnesslab/model/scripted.py
harnesslab/loop/state.py  harnesslab/tools/base.py
harnesslab/permissions/types.py  harnesslab/hooks/types.py  harnesslab/transcript/rows.py
harnesslab/agents/tasks.py  harnesslab/agents/notifications.py
tests/conftest.py  tests/test_smoke.py
pyproject.toml  README.md  ARCHITECTURE.md  WORKPLAN.md (WPs may append to "Change requests")
```
Exception: `harnesslab/tools/builtin/__init__.py` contains `default_tools()` and is **WP2-owned**.
`agents/workflow.chain_key` and `host/broker.digest` are pre-implemented pure helpers in
WP5 files; WP5 may keep or reimplement them (same behaviour).

---

## WP1 - Agent loop & model API

**Files (exclusive):**
`harnesslab/loop/query.py`, `harnesslab/loop/recovery.py`, `harnesslab/loop/nudges.py`,
`harnesslab/loop/normalize.py`, `harnesslab/api/retry.py`, `harnesslab/api/caching.py`,
`harnesslab/api/request.py`, `harnesslab/api/accounting.py`,
`harnesslab/model/anthropic_adapter.py`, `harnesslab/session.py`, `harnesslab/cli.py`.

**Spec to satisfy:** 01 R1-R59 (all of §3) and §4 (`run_query`, `recovery_ladder`,
`backoff_ms`, `is_retryable`, `shrink_for_overflow`, `place_message_breakpoint`,
`resolve_effort`); 01#T1-T12; 02 R30 (rendering attachments as reminders in
`normalize`), 02 R46-equivalent 01 R46; 05 R1 (use `tokens.context_tokens`); 04 R33
(UserPromptSubmit block erases prompt, in `session`); 05 R46 (snapshot per prompt, in
`session`); 07 R30 (CLI flags subset: `--ndjson`, `--model`, `--max-turns`,
`--permission-mode`). Patterns P01-P05, P10.

**Notes.** `run_query` must work with only `QueryDeps(call_model, build_request,
make_executor)` given (attachments / compactor / hooks / system_prompt may be `None`).
`default_deps` and `create_session_context` import other WPs lazily. `cli.demo_model()`
must work offline. Use `types.SYNTHETIC_RESULT_TEXT` for synthetic results.

**Tests to add:**
* `tests/test_wp1_loop.py` - text-only turn ends `completed`; tool turn → results in
  order → second round; transitions recorded; guards reset on next turn (R3);
  `max_turns` attachment + reason (R5); stop-hook blocking + cap 8 with a fake
  `HookRunner` (R6); abort during stream and during tools (R51) with a fake executor;
  mid-turn input folded (R54); budget exceeded (R8).
* `tests/test_wp1_recovery.py` - prompt-too-long → reactive compaction once via a stub
  `Compactor`, else `prompt_too_long` (R26); thrash breaker (R27); max_tokens nudges ×3
  and the reasoning-cut variant (R28); truncated stream for subagent (R29); malformed
  tool use retried once then exhausted (R30); thinking-only nudge once (R31).
* `tests/test_wp1_normalize.py` - merge consecutive users, drop empty text, lone
  surrogates, attachment rendering, pairing repair (dupe ids, orphan results, missing
  results get `MISSING` text, strict mode raises) (R55-R57).
* `tests/test_wp1_retry.py` - backoff bounds & retry-after (R18), retryability order
  (R19), overflow self-heal (R20), 529 foreground vs background + fallback after 3 (R21,
  R33), mid-stream table keeps partial after real output + caps (R15), tombstones on
  abandoned attempt (R16), interruptible sleep (R24), beta latching (R25) - all via
  `ScriptedModel` `fail(...)` turns.
* `tests/test_wp1_caching_request.py` - single breakpoint placement & skip rules,
  fork skip-write (R42); TTL order (R44); kill switches (R45); thinking/max_tokens
  clamps (R36-R37); effort resolution (R38); fast-mode cooldown (R39); cost incl. 1h/5m
  split and US multiplier (R50).
* `tests/test_wp1_session_cli.py` - `Engine.submit` with fake collaborators; `cli.main`
  runs one scripted exchange from a StringIO stdin.
* `tests/test_e2e_offline.py` - full stack with real tools/permissions/transcript and a
  ScriptedModel doing Read → Edit → Bash. Mark each test with a helper that
  `pytest.skip`s on `NotImplementedError` from another WP, so it turns green as WPs land.

---

## WP2 - Tools

**Files (exclusive):**
`harnesslab/tools/registry.py`, `harnesslab/tools/pipeline.py`,
`harnesslab/tools/executor.py`, `harnesslab/tools/ledger.py`, `harnesslab/tools/output.py`,
`harnesslab/tools/edit_match.py`, `harnesslab/tools/shell.py`,
`harnesslab/tools/builtin/__init__.py`, `harnesslab/tools/builtin/{read,write,edit,glob,grep,bash,todo,tool_search}.py`.

**Spec to satisfy:** 03 R1-R58 and R65-R66 (except R59-R64 tools not in scope: plan mode,
Skill, WebFetch, WebSearch, Monitor, LSP - list as Deferred), 03 §2.1-§2.4,
§4.1-§4.7; 03#T1-T12; 01 R9-R13 (executor side); 04 R30 (denial result text);
04 §4.5 (classifier projections for your tools); 05 R46 (call
`ctx.file_history.before_edit` when present). Patterns P11-P16.

**Notes.** Permissions come only from `call_ctx.session.can_use_tool` (fake it in
tests). Bash read-only analysis lives in WP3 (`permissions.bash_safety`) - import it
inside `BashTool.is_read_only` / `check_permissions`; in WP2 tests monkeypatch it.
Background work uses FINAL `ctx.tasks` / `ctx.notifications`.

**Tests to add:**
* `tests/test_wp2_executor.py` - safe calls run in parallel (barrier timing via
  events), unsafe call blocks later ones, results in original order, unknown tool error,
  parse-failure = unsafe, discard → synthetic results, concurrency cap, context layers
  deferred for safe runs (03 R9-R14, 01 R10-R12).
* `tests/test_wp2_pipeline.py` - malformed JSON echo, coercions with notes, schema error
  + ToolSearch hint for undiscovered deferred tool, validate_input ask/error code,
  deny path text, empty-result note (03 R1-R8).
* `tests/test_wp2_ledger.py` - not-read refusal, touch-only mtime with same hash OK,
  stale-recovered, real stale fails, read dedup stub, formatter note ≤5 files, LRU caps,
  subagent clone drops seeded (03 R17-R22).
* `tests/test_wp2_edit.py` - exact, curly-quote folding with re-curling, escape
  matching, desanitize, trailing-ws strip (non-md), newline deletion, multiple matches
  code 9, replace_all, multi-edit rules, BOM/CRLF preserved (03 R27-R33).
* `tests/test_wp2_output.py` - spill file + `<persisted-output>` preview cut at newline,
  symlink refusal → `<truncated-output>`, batch budget spills largest first and memo
  re-applies identically (03 R23-R26).
* `tests/test_wp2_shell.py` - cwd persists / env does not, control chars rejected,
  `sleep 30` rejected, timeout → background task + notification, head truncation, cwd
  outside allowed dirs reset (03 R38-R46). Skip on non-POSIX.
* `tests/test_wp2_builtin.py` - Read numbering/pagination banner/binary refusal/empty
  file reminder; Write report-file refusal for subagents; Glob ≤100 by mtime; Grep modes
  + head_limit footer (python fallback); TodoWrite clears when all done; ToolSearch
  `select:`, `+required`, scoring order, discovered set (03 §4.7).

---

## WP3 - Permissions, hooks, sandbox

**Files (exclusive):**
`harnesslab/permissions/rules.py`, `harnesslab/permissions/hazards.py`,
`harnesslab/permissions/bash_safety.py`, `harnesslab/permissions/modes.py`,
`harnesslab/permissions/classifier.py`, `harnesslab/permissions/pipeline.py`,
`harnesslab/hooks/matcher.py`, `harnesslab/hooks/runner.py`,
`harnesslab/pseudo/sandbox_profiles.py`.

**Spec to satisfy:** 04 R1-R48 and §4.1-§4.9 (all), 04#T1-T12; 03 R43 (read-only
analysis used for concurrency); 07 R17 (MCP rule syntax); 07 R35 (callback hooks shape);
06 R6 (`Agent(<type>)` deny rules parse correctly). Patterns P17-P20.

**Notes.** `make_can_use_tool(ctx)` is the only entry point WP2 calls. Prompt hooks and
the classifier call `ctx.model` / an injected `Model` - test with `ScriptedModel`.
Write the classifier policy prompt yourself from 04 §6's description.

**Tests to add:**
* `tests/test_wp3_rules.py` - parse/format incl. escaped parens and `Tool(*)`; Bash
  prefix / wildcard (`git *` matches `git`) / exact; gitignore file patterns; WebFetch
  domain; MCP server/wildcard rules; validator warnings (04 R7-R10, §4.2).
* `tests/test_wp3_bash_safety.py` - read-only allowlist & flags, git `-c` not read-only,
  redirects, subshell/expansion → not read-only, compound severity combine, forced asks
  (2× cd, cd+git), dangerous removal incl. `rm -rf $UNSET/`, sed `w`/`e` ask, prefix
  suggestions (04 R12-R17).
* `tests/test_wp3_pipeline_modes.py` - deny > ask > allow regardless of source; hook
  allow still denied by deny rule / safety check; hook ask forces prompt; each mode's
  residual-ask behaviour; bypass-immune breaker prompts in bypass; repo-controlled
  bypass ignored; plan mode save/restore; auto-mode allow-rule stripping; headless deny
  text (04 R1-R11, R19, R24, R30, R35, §4.1, §4.6).
* `tests/test_wp3_classifier.py` - verdict parsing fail-closed; stage-1 allow skips
  stage 2; stage-2 overturn; no-verdict streak 10; 3 consecutive / 20 total denials →
  prompt; projection omits reads and raw tool output (04 R20-R29, §4.4-§4.5).
* `tests/test_wp3_hooks.py` - matcher forms (exact list, regex, invalid regex), `if`
  filter, dedupe, parallel same input, exit 0 JSON / plain context / exit 2 / other,
  onFailure block, timeout fails closed, merge precedence, disableAllHooks /
  managed-only, command hooks via a tiny `python3 -c` script, callback hooks (04 R31-R40).
* `tests/test_wp3_sandbox.py` - darwin profile contains deny-default, write roots,
  protected literals and parents, network proxy lines; bwrap args; command tag round
  trip; network policy order; simulated sandbox decisions (04 R41-R46).

---

## WP4 - Context, compaction, transcript

**Files (exclusive):**
`harnesslab/context/prompt.py`, `harnesslab/context/attachments.py`,
`harnesslab/context/instructions.py`, `harnesslab/context/memory.py`,
`harnesslab/compaction/thresholds.py`, `harnesslab/compaction/controller.py`,
`harnesslab/compaction/summarizer.py`, `harnesslab/compaction/microcompact.py`,
`harnesslab/compaction/reinject.py`, `harnesslab/transcript/store.py`,
`harnesslab/transcript/checkpoints.py`.

**Spec to satisfy:** 02 R1-R38 and §4 (output styles R33-R34 handled as attachments +
`PromptFacts.output_style`; style *file loading* minimal), 02#T1-T12; 05 R1-R49 and
§4.1-§4.9 (R49 retention sweep may be Deferred), 05#T1-T12; 01 R41 (system block
split), 01 R27 / 05 R13 (thrash breaker), 03 R55 (deferred-tool delta attachment),
01 R8 (budget attachment). Patterns P05-P10, P21-P22.

**Notes.** The summarizer calls `ctx.model.stream(...)` directly with a
`ModelRequest(query_source="compact")` and FINAL `StreamAccumulator`; keep
`summarize_fn` injectable. The ledger snapshot comes from `ctx.ledger.snapshot()`
(duck-typed; fake it). Register attachment renderers in
`types.ATTACHMENT_RENDERERS` at import of `context.attachments`.

**Tests to add:**
* `tests/test_wp4_prompt.py` - shared section order, task-conduct omitted under a
  non-coding style, memoization by variant key, boundary split into global/org blocks,
  missing boundary → org, bare mode, override/append order, subagent notes (02 R1-R9, R35-R36).
* `tests/test_wp4_attachments.py` - environment first full then diff-only then nothing;
  model re-sent on change only; producers run in parallel and a slow/failing one is
  dropped at the 1 s deadline; todo reminder 10/10; plan reminders every 5, full every
  5th; total-tokens tag; renderers wrap in system-reminder (02 R10-R14, R30-R32).
* `tests/test_wp4_instructions_memory.py` - load order & labels, `@import` from text
  only (not code), depth 5, cycles, conditional rules on touched path, nested memory,
  excludes; MEMORY.md truncation; write lint refusals; recall caps & budget (02 R15-R29).
* `tests/test_wp4_compaction.py` - worked threshold example (200k/64k → 180k/167k/147k/
  144k/177k); controller breakers (3 failures, thrash, prefix too large); full compaction
  ordering per 05 R22 with a fake summarize_fn; PreCompact block; ptl_retry drop 20 %;
  reactive keeps recent rounds verbatim; micro-compaction keep 5 / ≥20k / file written /
  replacement replay; re-injection budgets (05 R5-R35, §4.1-§4.6).
* `tests/test_wp4_transcript.py` - append parent rules (tool results → assistant row),
  metadata re-appended at tail, load leaf selection & cycle guard, fork new id, resume at
  uuid, GC keeps the right rows, rewind reminder text (05 R36-R45, §4.7-§4.8).
* `tests/test_wp4_checkpoints.py` - copy-on-first-touch, null backup for new file,
  snapshot dedup, rewind restores/deletes, symlink/hardlink skipped, dry-run diffstat,
  100-snapshot cap (05 R46-R48, §4.9).

---

## WP5 - Agents, extensions, host, computer-use & desktop pseudo

**Files (exclusive):**
`harnesslab/agents/definitions.py`, `harnesslab/agents/launcher.py`,
`harnesslab/agents/agent_tool.py`, `harnesslab/agents/workflow.py`,
`harnesslab/extensions/skills.py`, `harnesslab/extensions/skill_tool.py`,
`harnesslab/extensions/mcp.py`, `harnesslab/host/protocol.py`, `harnesslab/host/broker.py`,
`harnesslab/pseudo/computer_use.py`, `harnesslab/pseudo/desktop_host.py`.

**Spec to satisfy:** 06 R1-R42 and §4.1-§4.5 (teams R31-R33, Monitor/Cron/Wakeup
R46-R53 and worktrees R54-R57 → Deferred, except pure helpers you choose to add),
06#T1-T12; 07 R1-R27, R30-R38 and §4.1-§4.5, §4.7-§4.8 (plugins/marketplaces §2.4,
statusline R29/§4.6, remote control R41 → Deferred), 07#T1-T12; 08 §2.4, §4.1-§4.6;
00 R2-R7 + §4; 09 §2-§4 (policy parts), 10 §4.1, §4.3. Patterns P23-P28.

**Notes.** The launcher runs children through `loop.query.run_query` (WP1); make the
runner injectable (constructor arg defaulting to a lazy import) and test with a fake
runner that drives a `ScriptedModel`. `host.protocol.HostServer` drives anything with
`submit()`/`interrupt()`; test with a fake engine and `io.StringIO`.

**Tests to add:**
* `tests/test_wp5_agents.py` - definition parsing & precedence, type resolution errors,
  depth/concurrency/budget refusals with model-readable text, async launch text +
  notification on completion, tool filtering table (never set, async allowlist, mcp
  family, zero tools refusal), fork construction (placeholders for every pending
  tool_use, no recursive fork), SendMessage continuation, TaskStop aliases (06 R1-R29).
* `tests/test_wp5_workflow.py` - chained keys; resume replays longest unchanged prefix
  then runs live; parallel/pipeline turn failures into None; 1000-call cap; malformed
  narrowing refused (06 R34-R41).
* `tests/test_wp5_skills_mcp.py` - discovery & namespacing & realpath dedupe; listing
  within budget degrades to name-only, bundled keep descriptions; resolution
  (exact/alias/suffix/ambiguous/edit-distance/user-only/already-running); inline context
  layers; fork via fake runner; MCP name normalisation, combinator flattening, bad schema
  dropped alone, description cap, output cap bands + footer, interrupted call message
  (07 R1-R27).
* `tests/test_wp5_host.py` - NDJSON encode/decode, control request/response matching,
  timeout, cancel, direction rejection, fail-all on close, `can_use_tool` round trip via
  `prompter`, `priority: now` preemption event, idle state signal; broker nonce mint/
  consume single-use, mismatch refused, only-on-card, session "always" cache (07 R30-R38,
  08 §4.2).
* `tests/test_wp5_pseudo.py` - screenshot `fit` (token/edge caps), `to_logical` mapping
  and out-of-frame teaching error, tier categories lower-only, request_access failure
  modes, action gate frontmost/tier/hit-test with `SimulatedDesktop`, batch stops at first
  error, AX unsupported cache + fail-closed unknown reasons, helper budgets/backoff;
  desktop host env, shim decline rules, worktree argv, append freezing, CI dedup, mention
  scorer (09, 10, 00, 08).

---

## Merge order & integration

WPs are independent; any merge order works. After all five land, WP1 removes the
skip helper from `tests/test_e2e_offline.py` and the architect runs the full suite.

*Outcome:* all five WPs landed green in isolation (756 tests). The integration pass
(below) resolved the change requests, removed the skip helper and added end-to-end
scenarios; the full suite is green.

## Spec gaps & design choices already made (architect)

| # | Gap | Decision |
|---|---|---|
| G1 | Spec describes async generators/JS runtimes | sync generator + threads; workflows are Python callables |
| G2 | 01 R57 media cap value not given | `MAX_MEDIA_BLOCKS = 100` (WP1 may tune) |
| G3 | 01 R15 full mid-stream table not enumerated | WP1 fills `MID_STREAM_POLICY` from the stated principle |
| G4 | Model catalog is remote (01 §8) | static `api.request.ModelInfo` table |
| G5 | 01 R49 vs 05 §4.1 block level differ (`window-3000` vs `context_window-reserve-3000`) | use 05 §4.1 (more precise) everywhere |
| G6 | 03 R51/R52 image resize & PDF render need non-stdlib | no resize (refuse oversize with hint); PDF → not-supported note |
| G7 | 04 §4.3 SAFE_FLAGS (~70) and PURE_UTILS (~50) not enumerated | seeded subset, extend conservatively |
| G8 | 04 R12 "real shell grammar" | `shlex`-based parser; anything unusual = too complex (fail safe) |
| G9 | Classifier / summary / section prose withheld | implementers author their own text from the stated purposes |
| G10 | In-process hook type for SDK hosts | added `callback` hook type |
| G11 | Ledger "fast non-crypto hash" unspecified | `zlib.crc32` + length |
| G12 | 02 R28 index-search scoring unknown; 07 skill priority unknown | simple keyword overlap; recency then name |
| G13 | 03 ToolErrorCode values (only 9 fixed) | own enum in `tools.base` |
| G14 | 01 R8 budget enforcement location unconfirmed | checked by the loop after each round; extra `TerminalReason.BUDGET_EXCEEDED` |
| G15 | MCP stdio/http transports, OAuth, elicitation | Protocol only; in-process (`sdk`) servers real |
| G16 | Out of scope for the first iteration | WebFetch/WebSearch/LSP/NotebookEdit/AskUserQuestion/plan-mode tools, Monitor, Cron, ScheduleWakeup, teams, worktrees, plugins/marketplaces, statusline, remote control, retention sweep |

## Change requests (append below; architect reviews)

*All requests below are resolved; the resolution of each is in the Integration log
after the list. The original text is kept unchanged.*

- WP1: `interfaces.ToolExecutor` has no accessor for a tool-requested turn end (01 R7, `ToolCallOutcome.end_turn`); `loop.query` reads it duck-typed from `executor.calls[i].outcome.end_turn`. Suggest adding `ToolExecutor.end_turn() -> str | None`.
- **WP4 -> WP1 (normalize):** attachment renderers registered by `context.attachments` return text *already wrapped* in `<system-reminder>`; wrap only when `not text.startswith(util.text.REMINDER_OPEN)`. Renderers are registered when `harnesslab.context.attachments` is imported (do so in `default_deps` / normalize).
- **WP4 -> WP1 (loop):** `CompactResult.items` holds fresh rows (new uuids, kept messages copied) - append all of them to `ctx.transcript`; the boundary keeps `parent_uuid=None` (the store enforces it). Set `ctx.extra["cache_safe_params"] = {"system": [...], "tools": [...]}` when building a main request so the summarizer can fork warm, and optionally `ctx.extra["prefix_tokens"]` (05 R12).
- **WP4 -> WP1/WP2 (counters):** `AttachmentPipeline.collect` ticks `ctx.counters` once per new `TurnInfo.turn_count`; nobody else should increment them. TodoWrite resets `turns_since_todo_write` to 0.
- **WP4 -> WP2 (ledger):** compaction calls `ctx.ledger.snapshot()` (falls back to `items()`, oldest->newest) and then `ctx.ledger.clear()` (05 R21) - please provide `clear()`. `edited_files_producer` reads `items()` entries' `content`, `partial_view`, `mtime` (s or ms).
- **WP2 -> WP1 (executor):** `StreamingToolExecutor` exposes `end_turn_request() -> str | None` (first `ToolCallOutcome.end_turn`; `calls[i].outcome.end_turn` also works), `outcomes()`, and an optional `on_context_layer` constructor callback that fires when a layer is *released* (serial: at once; safe: when the parallel run ends, 03 R12). `discard(reason)` returns every not-yet-emitted result in call order (real ones for finished calls) and marks them emitted. `drain_all` converts to `discard(INTERRUPTED)` after a 2 s grace once `ctx.abort` fires. `tools.output.enforce_batch_budget(results, ctx, tools_by_id)` mutates blocks in place; call it on each tool-result batch after `drain_all`.
- **WP2 -> WP3 (hook defer, 03 R7):** `tools.pipeline` treats a `Decision` whose reason is `HookReason(reason="defer")` as a PreToolUse "defer"; it is honoured only when headless (`permissions.headless` or `not is_interactive`) and the call is alone in its batch, else the decision's behaviour stands. Suggest `can_use_tool` return `Decision("allow"|"deny", HookReason(name, "defer"))` for hook defers. Without `ctx.can_use_tool`, the pipeline falls back to `Tool.check_permissions` (deny denies, else allow).
- **WP2 -> WP4 (ledger):** `ReadLedger.snapshot()` (most-recent-first copies) and `clear()` exist; `LedgerEntry.mtime` is in **milliseconds** (`st_mtime_ns // 1e6`). Registry: `ToolRegistry(ctx.tools).deferred_names(ctx.discovered_tools, remote_deferred=...)` for the 03 R55 delta attachment.
- **WP2 -> WP5 (ctx.extra keys read by tools):** `mcp_pending_servers` (set or callable; executor waits up to 5 s for `mcp__<server>__*` tools, ToolSearch polls), `mcp_server_status` (`{server: {"status": "pending|failed|blocked", "error": str}}` for ToolSearch zero-match text), `remote_deferred_tools`, `never_deferred_tools`, `tool_result_limits` (`{tool: chars}` spill override). Background shells register `kind="shell"` tasks in `ctx.tasks` (created lazily if `None`) with `stop=` set, and notify via `ctx.notifications.notify`.
- **WP2 -> WP3 (sandbox):** `ShellRunner` wraps the composed script with `ctx.extra["sandbox"] = (SandboxBackend, SandboxConfig)` when present (skipped for `dangerouslyDisableSandbox`).
- **WP3 → WP1 (loop):** please set `ctx.extra["conversation_items"] = items` each round (auto-mode
  classifier projection 04 §4.5 and Stop prompt hooks read it), and after a tool batch end the turn
  when `ctx.extra.pop("end_turn_request", None)` is set (auto mode unavailable / headless denial
  limit, 04 R22-R23). Include `permissions.classifier.AUTO_MODE_AGENT_GUIDANCE` in the prompt while
  in auto mode (04 R29; WP1 or WP4, whoever owns that section).
- **WP3 → WP2 (tools):** PreToolUse hooks run inside `can_use_tool` (04 §4.1 step 0) - do not run
  them again in `tools.pipeline`; their `additionalContext` is in
  `ctx.extra["pre_tool_hook_context"][tool_use_id]`. The permission pipeline runs
  `bash_safety.check_bash` for `Bash` itself (stricter of tool check and analysis wins), so
  `BashTool.check_permissions` may simply return passthrough. Shell sessions should source the files
  listed in `ctx.extra["hook_env_files"]` (04 R38 `CLAUDE_ENV_FILE`). `check_bash` gained optional
  kw-only `allow_rules=` / `working_dirs=` (backwards compatible).
- **WP5 -> WP1/WP4 (subagent prompts):** the launcher puts the agent's prompt + notes
  block in `child.extra["agent_system_prompt"]` and `child.extra["omit_claude_md"]`;
  forks copy the parent's `extra["rendered_system_prompt"]` verbatim (06 R26). The
  `SystemPromptBuilder` / `build_request` should use these when `ctx.agent_id` is set
  (and reuse `rendered_system_prompt` byte-for-byte when `ctx.is_fork`).
- **WP5 -> WP1 (fork context):** `AgentTool` builds forks from
  `ctx.extra["current_items"]`, falling back to WP3's `ctx.extra["conversation_items"]`.
- **WP5 -> WP1 (notifications):** agents and MCP auto-background notify through
  `ctx.notifications` themselves and set `task.meta["notified"] = True`; any generic
  `TaskRegistry.on_finish` listener should skip such tasks to avoid duplicates.
- **WP5 -> WP2 (tool pool):** `default_tools()` may add `AgentTool()`, `SendMessageTool()`,
  `TaskStopTool()` (agents.agent_tool) and `SkillTool()` (extensions.skill_tool); they
  share a launcher via `ctx.extra["subagent_launcher"]`. WP5 does not yet populate
  `mcp_pending_servers` / `mcp_server_status` (MCP connection management is deferred).
- **WP5 -> WP3 (optional):** workflow agents carry `child.extra["bash_command_clamp"]`
  (list of `Bash(...)` patterns, 06 R37) that the permission pipeline may enforce.


## Integration log (resolution of every change request)

Rule used when requests conflicted: pick the option most faithful to `spec/`, and
record the convention in `ARCHITECTURE.md §8`.

| # | Request | Resolution |
|---|---|---|
| a | Reminders double-wrapped (WP4 renderers wrap; WP1 normalize wrapped again) | `loop.normalize.as_reminder` wraps only unwrapped text; `ensure_renderers_registered()` imports `context.attachments` lazily so renderers exist even without `default_deps`. Test: `test_integration_seams::test_attachment_rendered_with_single_envelope`, e2e scenario 1. |
| b | PreToolUse hooks ran twice (WP3 `can_use_tool` + WP2 pipeline) | Already not run in `tools.pipeline`; now its `additionalContext` (`pre_tool_hook_context`) is appended to the tool result as a reminder. |
| c | Denial guidance appended by WP3 and WP2 | `tools.pipeline.denial_text` adds its own guidance only when WP3's `DENIAL_GUIDANCE` is absent (validator asks, bare embeddings). |
| d | Read-only tools (Glob, Read) prompted in default mode | `permissions.modes.read_only_fast_allow` + rule-pipeline step 3b: read-only tool (or inferred allowlist) inside the working dirs -> allow in every mode (04#T4); reads outside still ask (04 §4.4). |
| e | WP1 must publish ctx.extra keys, honour `end_turn_request`, record CompactResult rows | `loop.query.publish_items` (`conversation_items`, `current_items`) and `publish_request_params` (`cache_safe_params`, `prefix_tokens`, `rendered_system_prompt`); after a tool batch `end_turn_request` records a system row and ends `COMPLETED` (spec gives no reason enum); reactive-compaction rows are now emitted to the transcript like proactive ones. `SystemPromptBuilder` returns `rendered_system_prompt` verbatim for forks and uses `agent_system_prompt` for subagents; `instructions_producer` honours `omit_claude_md`. |
| f | `default_tools()` should include Agent/SendMessage/TaskStop/Skill | `default_tools()` = `core_tools()` + those four. Skills are discovered at session start (`extensions.skills.session_skill_roots`) and listed by a new `skill_listing_producer` (02 R37). |
| g | Notification dedupe via `task.meta["notified"]` | Background shells now tag it too; `session` registers a fallback `on_finish` listener that notifies untagged, non-killed tasks once. Agent notifications also name the agent id (the SendMessage handle). |
| h | TodoWrite resets `turns_since_todo_write` | Already done by WP2 (`tools/builtin/todo.py`). |
| i | Ledger API shape for WP4 | `snapshot()` / `clear()` / `items()` existed; fixed WP4's `edited_files_producer` comparing float seconds with the ledger's truncated milliseconds (every read file looked "changed"), and it now ignores touch-only / own-write changes. |
| j | Hook `defer` convention | WP3 `decide` returns `Decision("deny", HookReason(name, "defer"))`; WP2 honours it only headless and alone in the batch, else it stays a deny (03 R7, 04 §8). |
| k | Shell sources `hook_env_files` | `ShellRunner.compose` sources each existing file (errors ignored) before the command (04 R38). |
| l | AUTO_MODE guidance in auto mode | `SystemPromptBuilder` appends `AUTO_MODE_AGENT_GUIDANCE` as a section while the mode is `auto` (04 R29). |
| m | Engine / HostServer wiring for `--ndjson` | `cli.main --ndjson` builds the Engine, wraps it in `HostServer`, routes prompts through `server.prompter`; verified with piped NDJSON frames. |
| WP1 | `ToolExecutor` end-turn accessor | Loop prefers `executor.end_turn_request()` when present, duck-typed fallback kept. |
| WP2 | `enforce_batch_budget` + context layers | Loop applies the batch budget to the whole batch before recording it, then applies released layers via the new `loop.context_layers` (Skill grants / model / effort expire at the next user message, 07 R8). |
| WP3 | BashTool may return passthrough | `BashTool.check_permissions` returns passthrough when the session has `can_use_tool` (the pipeline runs `check_bash` itself) and runs the analysis itself otherwise, using `ctx.env["HOME"]`. |
| WP5 | `bash_command_clamp` (optional) | Enforced in `permissions.pipeline.rule_pipeline` step 1b: a Bash call no clamp pattern covers is denied (fail closed), even in bypass. |

Other integration fixes: the CLI passes the real environment to the session (shell
and platform facts in the environment attachment); the demo model summarises tool
results in one line; the terminal prompter shows the reason and honours
`default_to_no`.
