# 01 — Agent loop and model-API layer (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

## 1. Purpose & scope

This document specifies the core agent loop: how a turn is driven, how tools run while the response streams, how errors are retried, recovered or surfaced, and how each model request is built (thinking, effort, caching, output limits, normalization). It also covers context accounting, cost tracking, interrupts, and how a host such as the desktop app drives the engine.

Compaction algorithms are out of scope. Only their trigger points are covered here. System-prompt content is covered in spec 02.

## 2. Concepts & data model

### 2.1 Loop state
```
LoopState:
  messages:        list[Message]
  tool_ctx:        ToolContext         # tools, permissions, abort controller, executor
  compact_info:    CompactTracking     # turns since last compaction, refill streak
  guards:          Guards
  transition:      TransitionReason | None   # why we are iterating again
  turn_count:      int

Guards (all reset when transition == NEXT_TURN):
  output_limit_recoveries: int = 0       # cap 3
  tried_reactive_compact:  bool = False
  reactive_compact_depth:  enum{none, ...} = none
  thinking_only_nudged:    bool = False
  malformed_tool_retried:  bool = False
  stop_hook_block_count:   int = 0

QueryTrace: {chain_id: uuid, depth: int}   # depth +1 per loop round; attached to all telemetry
```

### 2.2 Enumerations

**TerminalReason** (the reason a turn ended):
- `completed`, `max_turns`
- `aborted_streaming`, `aborted_tools`
- `blocking_limit`, `rapid_refill_breaker`, `prompt_too_long`, `image_error`
- `model_error`, `api_error`, `malformed_tool_use_exhausted`
- `stop_hook_prevented`, `hook_stopped`, `tool_deferred`, `background_requested`

**TransitionReason** (the reason the loop continues):
- `next_turn`
- `max_output_tokens_recovery`, `truncated_response_recovery`
- `malformed_tool_use_retry`, `thinking_only_retry`
- `reactive_compact_retry`, `precomputed_compact_swap`
- `refusal_retry`, `stop_hook_blocking`

**SDK result subtype**: `success`, `error_during_execution`, `error_max_turns`, `error_max_budget_usd`, `error_max_structured_output_retries`.

### 2.3 Yielded events
The loop is an async generator. Everything it does is emitted to the consumer, which may be a terminal UI, an SDK JSON printer or a parent agent:
- `stream_request_start`;
- raw stream events;
- assistant messages and user/tool-result messages;
- attachments (for example `max_turns_reached{maxTurns, turnCount}` and `budget_usd`);
- `tombstone{message}`, which retracts an emitted partial row;
- system notices such as model switches;
- retry status events `{error, delay_ms, attempt, max_attempts}`.

### 2.4 API-error-as-message
A failed request does not raise out of the loop. It becomes an assistant message flagged `isApiErrorMessage`, carrying the error and the API-error detail. The loop inspects the last such row after the stream ends. It may **withhold** the row (not yield it) while it attempts recovery.

### 2.5 Meta messages
Recovery nudges and other synthetic prompts are user-role messages with `isMeta: true`. UIs and transcript viewers may hide them.

## 3. Behavioural requirements

### Loop structure
- **R1** The loop MUST be a single `while True` over a `LoopState`. Each iteration either returns a `TerminalReason` or replaces the state and continues with a `TransitionReason`. Real recursion MUST NOT be used. Every turn end SHOULD be logged with its terminal reason. *Confidence: observed.*
- **R2** Each iteration MUST run these steps in order:
  1. Fold queued commands, memory and attachments into the messages.
  2. Run proactive auto-compaction if due.
  3. Check the blocking context limit.
  4. Build per-turn reminders and resolve the model, effort and fast mode.
  5. Emit `stream_request_start`.
  6. Stream the model call while dispatching tools.
  7. Run the post-stream recovery ladder.
  8. If there was no tool use, run Stop hooks.
  9. Drain tool results, run post-batch hooks, and fold in mid-turn user input.
  10. Check max turns.
  11. Continue with `next_turn`.

  *Confidence: observed.*
- **R3** Guards MUST reset on every `next_turn`, so recovery budgets apply per model round. *Confidence: observed.*
- **R4** Some transitions change the next request. Example: an output-token override applies only when no transition is pending, so it is the first attempt of a round. *Confidence: observed.*

### Stop conditions
- **R5** `maxTurns` MUST be checked after tool results are collected, using `turn_count + 1 > maxTurns`. On trip, the loop emits a `max_turns_reached` attachment, runs end-of-turn hooks, and returns `max_turns`. The check MUST be repeated when a Stop hook keeps the turn alive. *Confidence: observed.*
- **R6** If the assistant reply contains no tool use, Stop hooks MUST run. If a hook returns a blocking error, the hook output is appended as user content and the loop continues with `stop_hook_blocking`. After `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` consecutive blocks (default 8), the engine MUST end the turn anyway and warn the user. The warning tells hook authors to check the `stop_hook_active` input field. *Confidence: observed.*
- **R7** A tool MAY end the turn, defer the turn (`tool_deferred`), or stop continuation (`hook_stopped`). *Confidence: observed.*
- **R8** A USD budget (`max_budget_usd`) MUST be exposed. The remaining budget SHOULD be given to the model as a context attachment, and exceeding it ends the session with `error_max_budget_usd`. *Confidence: observed (where enforcement happens is inferred).*

### Streaming and tool execution
- **R9** A tool MUST be queued for execution as soon as its tool-use block completes in the stream, without waiting for the end of the message. Completed results SHOULD be emitted during streaming on periodic drain ticks. *Confidence: observed.*
- **R10** Scheduling rules:
  - Concurrency safety MUST be decided **per call** from the parsed input. A tool is unsafe if its input does not parse.
  - A new call may start only if nothing is running, or if it and every running call are safe.
  - The queue MUST stop at the first unsafe queued call, so ordering is preserved.
  - Results MUST be emitted by walking the calls in their original order. A still-running unsafe call acts as a barrier: nothing after it is emitted until it finishes.
  - Batch parallelism is capped by `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` (default 10).

  *Confidence: observed.*
- **R11** A call to an unknown tool name MUST immediately produce an error tool result saying the tool is not available. *Confidence: observed.*
- **R12** Each tool gets a child abort controller linked to the turn's controller. Context mutations made by concurrency-safe tools MUST be deferred until the parallel batch finishes. *Confidence: observed.*

### Stream liveness
- **R13** Stream liveness MUST be monitored with the timers in the table below. *Confidence: observed.*

  | Mechanism | Behaviour |
  |---|---|
  | Heartbeat | A synthetic ping is emitted after 10 s of silence. |
  | Stall log | A gap of more than 30 s between events is logged. |
  | Idle watchdog | On by default (`CLAUDE_ENABLE_STREAM_WATCHDOG`). Warns at half the idle timeout and aborts at `max(CLAUDE_STREAM_IDLE_TIMEOUT_MS or server value, 300 000 ms)`. |
  | Byte-level idle timeout | 180 s for first-party traffic. |
  | First-byte timeout | `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS`, clamped to [10 s, 30 min]. |
- **R14** **Non-streaming fallback.** After a 404 on streaming, or after certain stream failures, the engine MUST retry with a non-streaming request, unless `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` is set. That request:
  - uses `API_TIMEOUT_MS`, or the server value, or 300 s by default (120 s in remote mode);
  - caps `max_tokens` at 64 000;
  - clamps the thinking budget to `max_tokens - 1`.

  The user is told that streaming ended early and that a non-streaming retry is under way. *Confidence: observed.*
- **R15** **Mid-stream failure policy.** The engine MUST decide what to do about a mid-stream failure with a table keyed on two things:
  - how far the stream got: nothing, started, partial output, thinking only, or real output;
  - the cause: overloaded, stalled, truncated, suspended, connection lost, server error, timed out, malformed, bad request, output filtered, or denied.

  The result is one of retry, retry without streaming, keep partial, use fallback model, or fail. Once real output has streamed, most causes MUST keep the partial output rather than re-bill. Per-attempt counters cap the special cases: stalls ≤ 1, truncations ≤ 1, retries after a thinking-only stream ≤ 2. *Confidence: observed.*
- **R16** When a streaming attempt is abandoned, every partial row already emitted MUST be retracted with a tombstone event. *Confidence: observed.*

### Retry engine
- **R17** Retry limits:
  - default 10 attempts;
  - `CLAUDE_CODE_MAX_RETRIES` overrides, clamped to 15 with a warning;
  - persistent (watchdog) mode, enabled by `CLAUDE_CODE_RETRY_WATCHDOG`, allows up to 300.

  *Confidence: observed.*
- **R18** Backoff: `delay = min(32 000, 500 · 2^(attempt-1))` ms plus up to 25 % jitter, raised to at least `retry-after` when that header is present. The overloaded base delay can be overridden with `CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS`. In non-persistent mode a `retry-after` above 60 s aborts. In persistent mode waits are capped at 6 h and capacity backoff at 5 min, and the status is re-emitted every 30 s. *Confidence: observed.*
- **R19** Retryability, evaluated in this order (first match wins):
  1. Hard credit or spend-cap 429s: never retry.
  2. Overloaded: retry.
  3. Context overflow: retry with an adjusted `max_tokens`.
  4. An `x-should-retry` header: obey it.
  5. Connection errors: retry.
  6. 408, 409 and 401: retry (401 refreshes credentials first).
  7. 429: retry only for non-subscribers, or when it is not a unified-quota rejection.
  8. Status ≥ 500: retry.

  The non-retryable set covers 401/403/404/407/413/429 after the rules above, plus prompt-too-long, billing and content-filter errors. If the status is non-retryable and a different fallback model is configured, the engine MUST trigger a last-resort fallback. *Confidence: observed.*
- **R20** **Context-overflow self-heal.** When the error reports `input + max_tokens > limit`, the engine sets `max_tokens = limit - input - 1000`. It fails if the result is below 3000 or no smaller than before. *Confidence: observed.*
- **R21** **Overload (529).** Background or auxiliary query sources MUST fail immediately on a 529. Foreground sources retry: the main interactive thread, SDK, subagents, compaction, hooks, side questions, web tools and auto-mode classification. After 3 consecutive 529s the engine switches to the fallback model if one is configured. Otherwise it fails with a repeated-overload error. *Confidence: observed.*
- **R22** Credential recovery happens inside the retry loop, with separate counters: OAuth refresh ≤ 2, cloud-provider credential refresh ≤ 2 each, API-key helper ≤ 2, and a workload-identity exchange on 401. *Confidence: observed.*
- **R23** After a stale-connection error, the next attempt MUST disable HTTP keep-alive. *Confidence: observed.*
- **R24** Retry status MUST be emitted as an event and a callback, so UIs can show a countdown. Retry sleeps MUST be interruptible by a wake signal. *Confidence: observed.*
- **R25** **Beta latching.** If the API rejects a beta capability header, the engine MUST stop sending it for the rest of the conversation and retry. Specific rejections trigger targeted retries:
  - an invalid thinking signature: strip all thinking blocks and retry;
  - mid-conversation system messages refused by a proxy: demote them to user-side reminders;
  - other cases: strip media, strip advisor blocks, or adjust prompt-end markers.

  *Confidence: observed.*

### Post-stream recovery ladder
- **R26** **Prompt too long.** Parse the actual and limit token counts from the error. Withhold the error. Try a reactive compaction, or swap in a precomputed compaction, then continue with `tried_reactive_compact = True`. If compaction is impossible, surface the error and return `prompt_too_long`. Oversized-image errors take the same path and end as `image_error`. *Confidence: observed.*
- **R27** **Compaction thrash breaker.** If context refills to the limit within 3 turns of the previous compaction, 3 times in a row, the engine MUST return `rapid_refill_breaker` with a message saying auto-compaction is thrashing. *Confidence: observed.*
- **R28** **Output-token limit.** On `stop_reason == max_tokens`, withhold the error row and send a continuation nudge, up to 3 times. The nudge MUST say:
  - resume immediately, with no apology and no recap;
  - continue mid-thought if that is where the cut fell;
  - split the remaining work into smaller pieces.

  If the limit was hit during reasoning, before any text or tool call, a different nudge MUST say that the reasoning was discarded and ask for much shorter reasoning. A third variant handles visible-text cuts that are rendered as a separate block: it asks the model to restart at the beginning of the interrupted line, row, list item or sentence, and to reopen any code fence or table. When the server supports thinking resumption, and at least 2048 tokens of headroom remain, the engine MAY resume an incomplete thinking block instead. The user-visible error names the limit and points at `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. *Confidence: observed.*
- **R29** **Truncated stream.** If the connection drops after output began, in non-interactive or subagent contexts, send a nudge asking the model to resume exactly where the text stopped, or to answer from scratch if nothing survived. Subagents instead get a nudge asking for a complete rewrite. *Confidence: observed.*
- **R30** **Malformed tool use.** If `stop_reason == tool_use` but no tool block parses, tombstone the reply and retry once with a nudge asking the model to emit the tool call again. A second failure ends with `malformed_tool_use_exhausted`. *Confidence: observed.*
- **R31** **Thinking-only end.** If the model ends the turn with thinking but no visible output, nudge once asking for a user-visible response. *Confidence: observed.*
- **R32** **Safety-classifier stop.** The engine MUST tell the model that the stop was a classifier decision, not a tool or API error, and that the content must not be produced again in any wording. *Confidence: observed.*

### Model selection and fallback
- **R33** A per-turn fallback chain MUST be built from the primary model and the configured fallbacks. When a fallback triggers, the engine:
  1. abandons the current attempt, tombstoning partial rows and discarding in-flight tools;
  2. advances to the next model;
  3. emits a model-change event and a system notice naming the old and new models and the reason.

  The reasons are `overloaded`, `server_error`, `model_not_found`, `permission_denied`, `model_blocked` and `last_resort`. `CLAUDE_CODE_NO_MODEL_FALLBACK` disables substitution. *Confidence: observed.*
- **R34** For refusal fallback, the server MAY reroute mid-stream. The client MUST then retract the refused rows by uuid, and MAY stitch salvaged partial text into the retry. *Confidence: observed (purpose inferred).*
- **R35** A model change made mid-turn takes effect at the next API call. *Confidence: observed.*

### Request configuration
- **R36** **Output limits.** The default max output is 32 000 tokens and the upper limit is 128 000, unless the model catalog says otherwise. Legacy models have smaller limits (4096 or 8192). `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is validated against the upper limit. The context window defaults to 200 000 tokens and is 1 000 000 for long-context model variants; `CLAUDE_CODE_DISABLE_1M_CONTEXT` turns the long window off. *Confidence: observed.*
- **R37** **Thinking.**
  - `max_tokens = min(explicit or recovery override or model max, model max)`.
  - If thinking is on and the model supports adaptive thinking, send the adaptive mode.
  - Otherwise send an explicit budget: `clamp(budget or upper_limit - 1, 1024, max_tokens - 1)`.
  - On first-party traffic, send an explicit "disabled" unless the model rejects that.
  - Kill switches: `CLAUDE_CODE_DISABLE_THINKING`, `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`, `MAX_THINKING_TOKENS=0`, and the `alwaysThinkingEnabled: false` setting.
  - A forced specific-tool `tool_choice` is demoted to `auto` while thinking is active.
  - The keyword "ultrathink" in a prompt is detected; it likely raises effort.

  *Confidence: observed (effect of keyword inferred).*
- **R38** **Effort.**
  - Levels: `low`, `medium`, `high`, `xhigh`, `max`.
  - Resolution order (first non-null wins): a hook-provided value, then `CLAUDE_CODE_EFFORT_LEVEL` ("unset" and "auto" mean null), then the per-turn value, then the session value, then defaults (remote config, then org default, then model-catalog default, then `high`).
  - The result is clamped by the `maxEffortLevel` setting or org policy.
  - `xhigh` and `max` are downgraded to `high` on models that lack them.
  - Effort is clamped to `high` when thinking is mechanically disabled.
  - It is sent as `output_config.effort` with the matching beta capability.

  *Confidence: observed.*
- **R39** **Fast mode.** Send `speed: "fast"` only on first-party traffic, for allowed models, when the org has opted in. On a 429 or 529 while in fast mode:
  - if `retry-after` is under 20 s, wait and stay in fast mode;
  - otherwise enter a cooldown of `max(retry-after or 30 min, 10 min)` and continue at normal speed.

  A 400 saying fast mode is unavailable or the parameter is unsupported permanently disables fast mode for that model and retries in place. *Confidence: observed.*

### Prompt caching
- **R40** A cache breakpoint is `{type: "ephemeral", ttl?, scope?}`. *Confidence: observed.*
- **R41** **System prompt split.** The system prompt is split at a private boundary sentinel:
  - before the boundary: one block cached at global scope (shared across organisations, because the text is identical for everyone);
  - after the boundary: org scope;
  - billing and identity header blocks: uncached.

  When MCP tools are present, the engine falls back to tool-based caching. *Confidence: observed (reason inferred: user-specific tool lists break a shared prefix).*
- **R42** **Message breakpoints.**
  - Normally there is exactly one marker, on the last *cacheable* message. Walking back from the end, skip system-role rows, request-scoped ephemeral user rows, and assistant rows whose last block is thinking or empty text.
  - The marker sits on the last content block of that message.
  - A fork that must not write cache moves the marker back one message.
  - An optional second marker can pin a fork point or the parent prompt end.

  *Confidence: observed.*
- **R43** Each request SHOULD estimate its uncached tail tokens and attribute them to a reason (proxy demotion, fork skip-write, unstampable tail and so on). *Confidence: observed.*
- **R44** **TTL selection** (first match wins):
  1. `FORCE_PROMPT_CACHING_5M` forces 5 m.
  2. `CLAUDE_CODE_PROMPT_CACHE_TTL` for main sources, or `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` for others.
  3. The `promptCacheTtl` or `subagentPromptCacheTtl` settings.
  4. Agent frontmatter.
  5. `ENABLE_PROMPT_CACHING_1H`.
  6. Default: 1 h for subscribers on main-thread, SDK, auto-mode or memory-relevance sources; 5 m for overage or usage credits and for API-key users.

  1 h requires the extended-TTL beta (`extended-cache-ttl-2025-04-11`). *Confidence: observed.*
- **R45** Caching kill switches: `DISABLE_PROMPT_CACHING` plus per-family variants (`DISABLE_PROMPT_CACHING_HAIKU`, `_SONNET`, `_OPUS`, and others). Throwaway side requests MAY ask the server to evict their cache on completion. *Confidence: observed.*
- **R46** **Mid-conversation system messages.** Per-turn reminders MAY be sent as `role: "system"` entries inside `messages`, optionally marked to clear at the next user message. If they are rejected, they MUST be demoted to user-side reminders. *Confidence: observed (rationale inferred).*

### Context accounting
- **R47** The engine MUST estimate context size as the last API usage total (input + cache creation + cache read + output) plus a character-based estimate for messages added since. Rows split from the same response collapse to the earliest index. *Confidence: observed.*
- **R48** Estimator:
  - text: about 4 characters per token (2 for JSON/JSONL);
  - each image or document: a flat 2000 tokens;
  - a tool use: name plus serialized input.

  Server-side token counting (`token-counting-2024-11-01`, with `max_tokens: 1`) MAY be used on some paths. *Confidence: observed.*
- **R49** Thresholds:
  - `effective = window - min(max_output, 20 000)`;
  - warn at `effective - 20 000`;
  - auto-compact at the compaction threshold;
  - block at `window - 3000`.

  At the block level the turn ends with `blocking_limit` before sending, unless reactive compaction is available. *Confidence: observed.*

### Cost
- **R50** Each model's price row holds USD per million tokens for input, output, 5-minute cache write, 1-hour cache write and cache read, plus a per-web-search price.
  - `cost = Σ tokens_k · price_k`, multiplied by 1.1 for US-only inference geography;
  - web searches cost $0.01 each;
  - a long-prompt tier applies above a token threshold;
  - unknown models use default pricing;
  - org policy may override prices.

  Final usage MUST come from the closing delta event rather than the stream-start values, and the 5 m / 1 h cache-creation split MUST be preserved. *Confidence: observed.*

### Interrupts
- **R51** There is one abort controller per turn and one child per tool. On abort, the engine:
  - converts pending tools into synthetic error results;
  - emits an interruption user message, with separate variants for "during the response" and "during tool use";
  - returns `aborted_streaming` or `aborted_tools`.

  *Confidence: observed.*
- **R52** A synthetic tool result MUST explain why the call has no real result. The reasons are listed below. *Confidence: observed.*
  - The turn ended to deliver a newer message.
  - The session ended before the result was recorded, so the outcome is unknown and the model should verify before re-running.
  - The call was skipped because a sibling call in the batch was denied.
  - The user declined the action, so the model should stop.
- **R53** For SDK sessions, or when `keepPartialMessageOnAbort` is set, partial text MUST be kept as an assistant row flagged as aborted mid-stream. *Confidence: observed.*
- **R54** User input that arrives mid-turn MUST be folded in between tool rounds as an attachment. The attachment says the user sent a message while the model was working, and asks the model to address it as it continues. *Confidence: observed.*

### Normalization before send
- **R55** Normalization rules:
  - Merge consecutive user messages and attachments.
  - Fold reminder text into adjacent blocks.
  - Drop empty or whitespace-only text blocks, and replace a fully empty message with a placeholder.
  - Strip lone surrogates.
  - Strip thinking blocks according to a strip plan, handling signatures from other models.
  - Remove advisor blocks when no advisor is configured.

  *Confidence: observed.*
- **R56** **Tool pairing repair.** Repair runs immediately before sending:
  - dedupe repeated tool-use ids;
  - drop orphan server or MCP tool uses and misplaced results;
  - add an error result for every unanswered tool use, saying it is missing because of an internal error;
  - remove results whose tool use is missing; on resume, replace a leading orphan with a note that it was removed;
  - substitute "interrupted" or "removed" placeholders so that thinking adjacency stays valid.

  Each repair is logged with a structural fingerprint. In strict mode, the engine MUST raise instead of repairing. *Confidence: observed.*
- **R57** When images or documents exceed a count or byte cap, the oldest MUST be replaced with a short "removed due to request limit" marker. *Confidence: observed.*
- **R58** Raw request and response logging MAY be enabled with `OTEL_LOG_RAW_API_BODIES` (`file:<dir>` or `inline`). Thinking content MUST be redacted in those logs. *Confidence: observed.*

### Host integration
- **R59** Hosts MUST drive the engine as a child process using `--output-format stream-json --verbose --input-format stream-json`. Options map to flags:
  - `--model`, `--fallback-model`
  - `--thinking`, `--max-thinking-tokens`, `--thinking-display`
  - `--effort`
  - `--max-turns`, `--max-budget-usd`, `--task-budget`

  Live changes travel over a control-request side channel: set model, set thinking budget, apply flag settings, get settings, interrupt. *Confidence: observed.*

## 4. Algorithms

```python
def run_query(state: LoopState):
    while True:
        state.messages = fold_pending_inputs(state)
        if should_autocompact(state):
            state.messages = yield from autocompact(state)
        if context_level(state.messages) == "blocked" and not can_reactive_compact(state):
            return end("blocking_limit")

        req = build_request(state)                        # model, effort, fast mode, reminders
        yield Event("stream_request_start")
        assistant, executor = [], state.tool_ctx.executor
        try:
            for ev in call_model_with_retries(req):
                if ev.kind == "fallback":
                    yield from tombstone_all(assistant); executor.discard()
                    req = req.with_model(next_model(state)); continue
                yield ev
                if ev.kind == "assistant_row":
                    assistant.append(ev.row)
                    for block in ev.row.tool_uses():
                        executor.add(block)
                yield from executor.drain_completed()
        except Aborted:
            yield from executor.synthesize_aborts(); yield interruption_message()
            return end("aborted_streaming")

        last = assistant[-1] if assistant else None
        outcome = recovery_ladder(state, last)            # may return Continue(reason, extra_msgs) or Terminal
        if outcome:
            if outcome.terminal: return end(outcome.reason)
            state = state.next(transition=outcome.reason, append=assistant + outcome.extra)
            continue

        if not any_tool_use(assistant):
            hook = run_stop_hooks(state)
            if hook.blocking:
                state.guards.stop_hook_block_count += 1
                if state.guards.stop_hook_block_count >= STOP_HOOK_BLOCK_CAP:
                    warn_user_stop_hook_cap(); return end("completed")
                state = state.next("stop_hook_blocking", append=assistant + hook.as_user_content())
                if over_max_turns(state): return end("max_turns")
                continue
            return end("completed")

        results = yield from executor.drain_all()
        run_post_batch_hooks(results)
        extra = take_midturn_user_input()
        if max_turns and state.turn_count + 1 > max_turns:
            yield Attachment("max_turns_reached", max_turns, state.turn_count)
            return end("max_turns")
        state = LoopState(messages=state.messages + assistant + results + extra,
                          guards=Guards(), transition="next_turn",
                          turn_count=state.turn_count + 1, ...)

def recovery_ladder(state, last):
    g = state.guards
    if is_prompt_too_long(last):
        if not g.tried_reactive_compact and compaction_possible(state):
            g.tried_reactive_compact = True
            return Continue("reactive_compact_retry", reactive_compact(state))
        surface(last); return Terminal("prompt_too_long")
    if last.stop_reason == "max_tokens" and g.output_limit_recoveries < 3:
        g.output_limit_recoveries += 1
        return Continue("max_output_tokens_recovery", [meta(nudge_for_cut(last))])
    if stream_truncated(last) and noninteractive_or_subagent(state):
        return Continue("truncated_response_recovery", [meta(truncation_nudge(state))])
    if last.stop_reason == "tool_use" and not parseable_tool_block(last):
        if g.malformed_tool_retried: return Terminal("malformed_tool_use_exhausted")
        g.malformed_tool_retried = True
        return Continue("malformed_tool_use_retry", [meta(RETRY_TOOL_CALL)], tombstone=last)
    if thinking_only(last) and not g.thinking_only_nudged:
        g.thinking_only_nudged = True
        return Continue("thinking_only_retry", [meta(ASK_FOR_VISIBLE_OUTPUT)])
    return None

class ToolExecutor:
    def can_start(self, call):
        if not self.running: return True
        return call.safe and all(r.safe for r in self.running)
    def pump(self):
        while self.queue and len(self.running) < MAX_CONCURRENCY:
            call = self.queue[0]
            if not self.can_start(call): break        # preserves order behind an unsafe call
            self.queue.pop(0); self.start(call)
    def drain_completed(self):
        for call in self.in_order:                     # walk in original call order
            if call.yielded: continue
            if call.done:
                call.yielded = True; yield call.result
            elif not call.safe:
                break                                  # an unsafe call still running is a barrier
            # an unfinished safe call does not block later finished safe results
    def add(self, block):
        tool = self.tools.get(block.name)
        if tool is None:
            return self.finish(block, error_result("tool not available"))
        try: safe = tool.is_concurrency_safe(tool.parse(block.input))
        except ParseError: safe = False
        self.queue.append(Call(block, safe)); self.pump()

def backoff_ms(attempt, retry_after_s=None, base=500, cap=32_000):
    d = min(cap, base * 2 ** (attempt - 1))
    d += random.uniform(0, 0.25 * d)
    if retry_after_s is not None: d = max(d, retry_after_s * 1000)
    return d

def is_retryable(err, ctx):
    if err.status == 429 and err.is_hard_credit_limit: return False
    if err.is_overloaded: return True
    if err.is_context_overflow: return True
    if (h := err.headers.get("x-should-retry")) in ("true", "false"): return h == "true"
    if err.is_connection_error: return True
    if err.status in (408, 409, 401): return True
    if err.status == 429: return not ctx.is_subscriber or not err.is_unified_quota
    return err.status is not None and err.status >= 500

def shrink_for_overflow(input_tokens, limit, prev_max):
    new = limit - input_tokens - 1000
    if new < 3000 or new >= prev_max: raise ContextOverflowUnrecoverable
    return new

def place_message_breakpoint(messages, skip_write=False):
    i = len(messages) - 1
    while i >= 0 and not cacheable(messages[i]):     # skip system rows, ephemeral users, thinking-tail assistants
        i -= 1
    if skip_write: i -= 1
    if i >= 0: messages[i].content[-1].cache_control = {"type": "ephemeral", "ttl": ttl()}

def context_level(msgs, window, max_out, autocompact_at):
    used = last_usage_total(msgs) + estimate_tokens(msgs_after_last_usage(msgs))
    effective = window - min(max_out, 20_000)
    if used >= window - 3000: return "blocked"
    if used >= autocompact_at: return "compact"
    if used >= effective - 20_000: return "warn"
    return "ok"

def resolve_effort(hook, env, turn, session, defaults, cap, model, thinking_disabled):
    e = hook or (None if env in (None, "unset", "auto") else env) or turn or session or defaults or "high"
    e = min_level(e, cap)
    if e in ("xhigh", "max") and not model.supports(e): e = "high"
    if thinking_disabled: e = min_level(e, "high")
    return e
```

## 5. Constants & knobs

| Item | Value |
|---|---|
| Output-limit recovery nudges per round | 3 |
| Stop-hook block cap | 8 (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`) |
| Tool concurrency | 10 (`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`) |
| Heartbeat ping | 10 s |
| Stall log threshold | 30 s |
| Stream idle abort | ≥ 300 s (`CLAUDE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_ENABLE_STREAM_WATCHDOG`) |
| Byte idle timeout (first party) | 180 s |
| First-byte timeout clamp | 10 s – 30 min (`CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS`) |
| Non-streaming timeout | 300 s (120 s remote) or `API_TIMEOUT_MS` |
| Non-streaming max_tokens cap | 64 000 |
| Max retries / clamp / persistent | 10 / 15 / 300 (`CLAUDE_CODE_MAX_RETRIES`, `CLAUDE_CODE_RETRY_WATCHDOG`) |
| Backoff base / cap / jitter | 500 ms / 32 s / ≤ 25 % |
| Retry-after abort (non-persistent) | > 60 s |
| Persistent wait cap / capacity cap / status cadence | 6 h / 5 min / 30 s |
| Consecutive 529s before fallback | 3 |
| Overflow floor / safety margin | 3000 / 1000 tokens |
| Fast mode short-retry threshold / default cooldown / min cooldown | 20 s / 30 min / 10 min |
| Default / upper output tokens | 32 000 / 128 000 |
| Context window default / long-context | 200 000 / 1 000 000 |
| Thinking budget floor | 1024 |
| Thinking-resumption headroom | 2048 tokens |
| Output reserve / warn margin / block margin | min(max_out, 20 000) / 20 000 / 3000 |
| Thrash breaker | refill within 3 turns, 3 times |
| Chars per token (text / JSON) | 4 / 2 |
| Image/document token estimate | 2000 |
| US-geo multiplier / web-search price | ×1.1 / $0.01 |
| Effort levels | low, medium, high, xhigh, max |

## 6. Rationale

- A flat generator loop with explicit transitions makes every recovery observable, and makes it easy to test as a state machine.
- Treating errors as messages lets the loop try cheap recoveries (compaction, nudges) before bothering the user. Per-round guard budgets prevent infinite loops.
- Starting tools during streaming hides tool latency behind generation time. Per-call safety allows parallel reads while serialising writes.
- Keeping partial output after real streaming avoids paying twice for work already done.
- Shedding background traffic during overload protects interactive users.
- Cache design (a global static prefix, a single rolling breakpoint, and removable reminders) maximises reuse across turns and users.

## 7. Harness design takeaways

- **T1 Generator loop with explicit transitions.** Use one `while True` over a replaced state object, and yield everything. Every continuation carries a named reason and every exit a terminal reason, logged per turn.
- **T2 Errors as withheld messages.** Represent API failures as flagged assistant rows. Hold them back while attempting compaction or continuation, and surface them only if recovery fails. Recovery budgets reset each round.
- **T3 Terse, specific continuation nudges.** Tell the model to resume without apology or recap, continue mid-thought, and reopen any interrupted fence. Mark nudges meta so they can be hidden, and cap them at 3.
- **T4 Stream-time tool start with per-call safety.** Decide concurrency safety for each invocation. Run safe calls in parallel up to 10, let unsafe calls block the queue, emit results in order, and apply parallel context changes after the batch.
- **T5 Always-paired tool calls with explanatory placeholders.** Repair pairing just before sending. Each synthetic result states why it is missing, to avoid duplicate side effects and false refusals. Keep a strict mode that fails in CI.
- **T6 Retry as a policy table.** Classify the error (status, retry headers, overload, overflow) and the stream progress. Map the pair to retry, non-streaming retry, keep partial, fallback, or fail. Keep partial output once real output exists, and self-heal overflow by shrinking `max_tokens`.
- **T7 Shed background load under overload.** Drop auxiliary requests on 529 immediately. Reserve retries and fallback for foreground sources, and tag every request with its source so policy can route on it.
- **T8 Cache-first prompt layout.** Put the static prompt behind a boundary at global scope and the dynamic parts after it. Keep one rolling breakpoint on the last stable message, send reminders as removable system messages, and measure and attribute the uncached tail.
- **T9 Economic TTL choice.** Use 1 h for long subscription main threads and 5 m for metered or short-lived subagent work, with env and settings overrides. Evict the cache for one-shot side requests.
- **T10 Latched, negotiable betas.** Drop any beta the API refuses for the rest of the conversation and retry. Keep the set stable across turns to avoid cache-key churn.
- **T11 Anchor context counts on server usage.** Add a crude local estimate only for new rows, keep an output reserve and a hard block margin, and add a thrash breaker for compaction.
- **T12 One engine, many front-ends.** Drive the same engine over a bidirectional JSON stream plus a control channel. Expose turn and budget limits as flags, and feed the remaining budget back to the model.

## 8. Uncertainties

- The auto-compaction and micro-compaction algorithms themselves were not traced. Only thresholds and triggers are specified.
- The normalization step has many more fine-grained rules than are listed in R55.
- The matrix of models that support adaptive or explicit thinking comes from a remote catalog. Only legacy limits are static.
- The end-to-end semantics of refusal fallback (R34) are inferred.
- Where exactly the USD budget is enforced was not confirmed.
- Thinking resumption, server-driven context hints and threaded continuation were seen only through retry tags.
