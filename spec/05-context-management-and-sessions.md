# 05 — Context management & session persistence (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

This document specifies how a coding-agent harness keeps a long-running conversation inside a fixed model context window, and how it persists, resumes, forks and rewinds sessions. It covers:

- token accounting and the threshold model ("how full is the window");
- a layered ladder of context-pressure relief, from cheap and reversible to expensive and lossy;
- full, reactive, partial, precomputed and idle compaction, and what is re-injected after a compaction;
- the on-disk transcript format (append-only JSONL with a uuid parent DAG), its garbage collection, and resume/fork/side-chat semantics;
- file checkpointing and rewind;
- retention.

Out of scope: the system-prompt assembly (see 02), the permission classifier (04), and subagent mechanics (06), except where they touch compaction.

---

## 2. Concepts & data model

### 2.1 Relief ladder (ordered cheapest → most lossy)

| Layer | Who acts | Lossiness |
|---|---|---|
| L1 Server-side context edits (thinking clearing; idle tool-use clearing) | API server, requested by client | Reversible from client's persisted transcript |
| L2 Client tool-result clearing ("micro-compaction") | Client | Lossless: cleared content spilled to disk and referenced by path |
| L3 Precomputed summary | Background fork | Lossy, but latency hidden |
| L4 Threshold ("auto") compaction | Client, before a request | Lossy summary + deterministic re-injection |
| L5 Reactive compaction | Client, after an overflow error | Lossy for oldest rounds; recent rounds verbatim |
| Side paths | Idle compaction (before cache TTL expiry); manual compaction; partial compaction from rewind menu | — |

### 2.2 Window record

```
WindowInfo {
  window: int            # tokens the harness treats as the usable context window
  configured: bool
  source: "env" | "settings" | "server" | "experiment" | "model-default" | "auto" | "unknown-model"
}
Levels {
  effective, warn, threshold, blocked, precompute: int
  state: "ok" | "warn" | "compact" | "blocked"
  pct_left: float        # (threshold - tokens) / threshold
}
```

### 2.3 Transcript row envelope (JSONL)

Every conversation row is one JSON object per line. Field names are interface-level and MUST be preserved for compatibility with existing tooling:

```
Row {
  type: "user" | "assistant" | "system" | "attachment" | <metadata types…>
  uuid: str
  parentUuid: str | null
  logicalParentUuid?: str       # set on compact boundaries: the pre-compaction leaf
  isSidechain: bool             # true for subagent transcripts
  timestamp: iso8601
  message?: { role, content, … }   # API-shaped message payload
  isMeta?: bool
  isCompactSummary?: bool
  toolUseResult?: any           # structured tool output
  sourceToolAssistantUUID?: str # tool_result rows parent to the assistant row that issued the call
  sessionId, cwd, version, gitBranch, entrypoint, userType, sessionKind, slug
  agentId?, agentName?, teamName?, promptId?
}
```

Metadata row types and their fold rule when reading:

| Fold rule | Row types |
|---|---|
| transcript (forms the DAG) | user, assistant, system, attachment |
| last-wins | summary, custom-title, ai-title, tag, mode, permission-mode, worktree-state, cost-state, pr-link, agent-name / agent-color / agent-setting |
| accumulate | content-replacement, fork-context-ref, memory-mode |
| cleared at a boundary | progress, file-history-snapshot, file-history-delta, last-prompt, api-request rows |

### 2.4 Compact boundary

```
CompactBoundary (system row) {
  parentUuid: null
  logicalParentUuid: <old leaf uuid>
  compactMetadata: {
    trigger: "auto" | "manual"
    preTokens, postTokens, durationMs: int
    preCompactDiscoveredTools: [str]          # tool-search state to restore
    preservedSegment?: { headUuid, anchorUuid, tailUuid }
  }
}
```

### 2.5 File-history snapshot

```
Snapshot {
  messageId: str
  trackedFileBackups: { <abs path>: {
      backupFileName: str | null,   # null ⇒ file did not exist ⇒ delete on rewind
      version: int,
      backupTime: iso8601,
      realParentDir: str            # realpath of parent at backup time (link safety)
  } }
  timestamp: iso8601
}
```

Backups live at `<configDir>/file-history/<sessionId>/<hex16(sha256(abs_path))>@v<n>`.

### 2.6 Paths

- Session transcript: `<configDir>/projects/<slug>/<sessionId>.jsonl`.
- Slug: the cwd with every non-alphanumeric character replaced by `-`; if longer than 200 characters, truncate and append a short base-36 hash.
- Subagent transcript: `<configDir>/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl`.
- Remote-agent metadata: `<sessionId>/remote-agents/remote-agent-<id>.meta.json`.

---

## 3. Behavioural requirements

### Token accounting
- **R1** The live token count MUST be anchored on the most recent assistant message carrying real API usage (input + cache-read + cache-creation + output), plus a character-based estimate of every row after it. A compact boundary resets the anchor to zero. *Confidence: observed.*
- **R2** The estimator SHOULD be `round(len(text) / bytes_per_token)` with 4 bytes/token by default, 3 for some model families, 2 for JSON/JSONL content; each image counts as 2000 tokens. *Observed.*
- **R3** Tokens reported as cleared by server-side tool-use clearing in the previous response MUST be accounted for so that the client does not double count. *Observed.*
- **R4** A "large turn" flag SHOULD be raised when the last assistant usage exceeds 200,000 tokens. *Observed.*

### Window resolution
- **R5** The window MUST be resolved with this precedence: (1) env `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, clamped to [100k, 1M]; (2) user setting (per model, falling back to a default entry); (3) server-delivered client configuration; (4) experiment configuration; (5) model default (models billed differently above 200k are clamped to 200k; some large-window models default to a smaller window on certain surfaces); (6) the model's advertised context window. *Observed.*
- **R6** Thresholds MUST be computed from the effective window (§4.1). *Observed.*
- **R7** `DISABLE_COMPACT` MUST disable all compaction including manual. `DISABLE_AUTO_COMPACT` or setting `autoCompactEnabled: false` MUST disable only the automatic path. *Observed.*
- **R8** In remote-worker deployments the threshold MAY be marked "not enforced": the harness waits for the API's prompt-too-long error and routes to reactive compaction; thin clients are told via a state frame that enforcement is off. *Observed.*
- **R9** The UI SHOULD display "N% until auto-compact" when enforced, "N% context used" otherwise, and a low-context warning suggesting manual compaction once past `warn`. *Observed (paraphrased).*

### Auto-compaction control
- **R10** The control loop runs once per turn before the API request (§4.2). *Observed.*
- **R11** After 3 consecutive compaction failures, auto-compaction MUST be skipped for the rest of the session. *Observed.*
- **R12** If the fixed prefix (system prompt + tool definitions) alone exceeds the threshold, the harness MUST record that compaction cannot help, rather than loop. *Observed.*
- **R13** Thrash breaker: if context refills to the threshold within fewer than 3 turns after a compaction, 3 times in a row, auto-compaction MUST stop and the user MUST be shown an actionable message (a file or tool output is probably too large; read smaller pieces or clear the session). *Observed.*
- **R14** If the transcript's preferred model has a larger window than the current one and the current state is `blocked`, the harness MAY compact using the larger-window model. *Observed.*
- **R15** If the auto-permission classifier's own context overflows, the harness MUST force a compaction and inject a high-priority reminder asking the model to re-issue the tool calls that were dropped. *Observed.*

### Full compaction
- **R16** PreCompact hooks MUST run first with trigger `auto|manual`; a hook MAY block compaction or contribute extra instructions, which are merged with any user-supplied `/compact <instructions>`. *Observed.*
- **R17** The summary request MUST forbid tool use (a text-only preamble plus a closing reminder) and SHOULD be sent as a cache-sharing fork (§4.3). A cold fallback MUST exist. *Observed.*
- **R18** If the summary call itself overflows, the harness MUST drop the oldest API rounds and retry, up to 3 attempts (§4.4). *Observed.*
- **R19** The summarizer's reasoning scratch section MUST be stripped from the final summary; the summary section is labelled and wrapped into a continuation message that points to the on-disk transcript path. *Observed.*
- **R20** For automatic/reactive compaction, the continuation message MUST instruct the model to resume the work directly without asking the user questions, acknowledging the summary or recapping. *Observed (paraphrased).*
- **R21** After summarizing: snapshot and clear the read-file cache; rebuild post-compaction attachments (§4.5); write a compact boundary row; fire SessionStart hooks with source `compact`; then PostCompact hooks. *Observed.*
- **R22** The resulting message list MUST be ordered: boundary, summary (flagged as compact summary and transcript-visible only), kept messages, re-injected attachments, hook results. *Observed.*

### Summary content requirements (prompt purpose, not text)
- **R23** The default summary prompt MUST ask the model to first reason privately, then produce a structured summary covering: the user's primary request and intent; key technical concepts; files and code sections touched (with relevant snippets); errors and how they were fixed; problem-solving state; a complete list of the user's own messages; pending tasks; current work; and an optional next step anchored with direct quotes from the latest exchange to prevent drift. *Observed.*
- **R24** The prompt MUST require that user-stated constraints, especially security-relevant ones, are carried over exactly, and MUST forbid attributing to the user any text that merely looks like a user turn inside assistant or tool content. *Observed.*
- **R25** Alternative prompt variants MAY exist: a "lean" variant that tells the model precisely what the next window will already contain (system prompt and memory files; up to five recently read files; plan, loaded skills, background-task status; the transcript path) so it can keep pointers instead of content and quote limits exactly; a "short" variant with a brief preservation checklist; a word-capped variant. Selection is by configuration; the default is the full structured variant. *Observed.*

### Reactive / partial / precompute / idle
- **R26** Reactive compaction MUST group messages into API rounds and summarize only the oldest rounds, keeping the most recent rounds verbatim, sized from the server-reported overflow gap; the kept tail MUST be labelled as verbatim and unseen by the summarizer. The summary MUST be written so the model continues without follow-up questions. *Observed.*
- **R27** Reactive compaction is used on a real prompt-too-long error, on manual compaction with no custom instructions, or when auto is routed reactively. *Observed.*
- **R28** Partial compaction ("summarize from here" / "summarize up to here") MUST use a variant prompt that tells the summarizer which portion it covers and where the summary will be placed; an optional user note is appended as context; `preservedSegment` records the kept rows. *Observed.*
- **R29** Precompute (setting `precomputeCompactionEnabled`): once tokens pass the precompute line, a background fork SHOULD compute the summary in advance, persist it to a sidecar, and swap it in when the threshold is hit, unless the conversation head changed. *Observed; validity rules partly inferred.*
- **R30** Idle compaction (setting `idleCompaction`): if the last request used a 1-hour cache TTL, the session holds ≥200k tokens (configurable floor ≥100k via `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`) and the prefix is unchanged, a timer SHOULD compact at 90% of the remaining TTL, while the cache is still warm. *Observed.*

### Clearing / context editing
- **R31** When thinking is enabled, requests SHOULD carry a server context edit that keeps all thinking blocks (explicit policy). *Observed.*
- **R32** When the user returns after more than 3900 s (65 min) idle, requests SHOULD carry a server tool-use-clearing edit that keeps the last 5 tool uses and clears at least 20k input tokens, restricted to re-fetchable tools (read/search/shell/web/edit family). *Observed; exact tool set partly inferred.*
- **R33** Client micro-compaction MUST replace old clearable tool results with a stub, keep the most recent 5, and act only if at least 20k tokens are saved. Before clearing, each result MUST be written to disk and the stub MUST give the file path and tell the model to use the read tool. *Observed.*
- **R34** Every content replacement MUST be logged as a `content-replacement` row so a resumed session reproduces byte-identical request content (stable cache prefix). *Observed; rationale inferred.*
- **R35** Optionally, the main thread MAY advertise a "context hint" (target savings 75k when ≥20k is clearable). If the server rejects the request as would-overflow (HTTP 422/424), the client applies micro-compaction and retries the same turn without summarizing. *Observed.*

### Transcript storage
- **R36** `parentUuid` is the previous non-progress row, except that a tool_result row parents to its issuing assistant row, which permits parallel branches. *Observed.*
- **R37** Metadata rows (title, tag, last-prompt `{leafUuid}`) MUST be re-appended at the file tail so a session picker can read only the first and last 64 KiB. *Observed.*
- **R38** Transcript GC: after a boundary is followed by a real message, a rewrite MAY be requested; it keeps rows after the last boundary, the preserved segment, any rows referenced as dangling parents, and the latest last-wins metadata. It runs only when the file is ≥5 MiB, with a backstop every 20 MiB appended, doubling up to 160 MiB if a rewrite saves <10%. Local GC MAY be opt-in (`CLAUDE_CODE_TRANSCRIPT_LOCAL_GC`). *Observed.*
- **R39** Persistence MUST be disabled by `--no-session-persistence`, `CLAUDE_CODE_SKIP_PROMPT_HISTORY`, or a nested-session marker. *Observed.*

### Resume / fork / side chats / titles
- **R40** Leaf selection on load (§4.7). A parent-cycle detector MUST guard the chain walk. *Observed.*
- **R41** The resume picker MUST hide sidechains, team sessions, daemon sessions, SDK-entrypoint sessions, loop sessions, sessions superseded by a continuation, and same-slug sessions from a different real cwd; preview of first prompt capped at 200 chars. *Observed.*
- **R42** `--session-id` MUST NOT combine with `--continue`/`--resume` unless `--fork-session` is also passed. A transcript whose session id cannot be adopted silently becomes a fork. *Observed.*
- **R43** Rewind in a host = resume at an earlier uuid + fork. The host SHOULD keep rewind edges so a rewind can be undone, and MUST inject a reminder telling the model the conversation was rewound and whether files on disk were or were not reverted (advise checking VCS status). *Observed.*
- **R44** Side chats (`/btw`) MUST fork the main conversation with identical cache-safe parameters, one turn, no transcript write, no cache write, all tools denied, synthetic results for unanswered tool calls, and a reminder that it is a lightweight side agent with no tools that must answer directly without announcing actions. Side Q&A history is kept separately and MAY be promoted to a real branch. *Observed.*
- **R45** AI titles: generated from the last ~1000 chars of user/assistant text (optionally head+tail up to 2000); skip if input <10 chars; structured output `{title}`; ask for a 2–5 word sentence-case noun phrase (no leading verb, no generic "investigation"-style endings). Stored as `ai-title`; manual rename writes `custom-title`. *Observed.*

### Checkpoints & rewind
- **R46** File checkpointing is on by default (setting `fileCheckpointingEnabled`), disabled by `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING`, opt-in in SDK mode via `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING`. *Observed.*
- **R47** Only edits made through the harness's edit tools are tracked; shell-made changes are not, and the rewind reminder MUST say so. *Observed.*
- **R48** Rewind menu options: restore code and conversation; restore conversation only; restore code only; summarize from here; summarize up to here; cancel. A dry run MUST compute a diffstat for display. *Observed.*

### Retention
- **R49** `cleanupPeriodDays` (default 30, minimum 1) deletes transcripts and ~40 other artifact classes (file history, plans, shell snapshots, debug logs, session env, todos/tasks, uploads…). The sweep MUST be skipped if any settings file fails to parse (the value is unknowable); policy settings win. Desktop sessions are exempt unless `desktopSessionCleanupPeriodDays` is set. *Observed.*

---

## 4. Algorithms

### 4.1 Thresholds

```python
OUTPUT_RESERVE_CAP = 20_000
AUTOCOMPACT_BUFFER = 13_000
WARN_GAP           = 20_000
BLOCK_BUFFER       = 3_000
PRECOMPUTE_FRAC    = 0.20

def levels(window, context_window, max_output, tokens, pct_override=None, block_override=None):
    reserve   = min(max_output, OUTPUT_RESERVE_CAP)
    effective = window - reserve
    threshold = effective - AUTOCOMPACT_BUFFER
    if pct_override is not None:                       # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE
        threshold = min(floor(effective * pct_override / 100), threshold)
    warn      = threshold - WARN_GAP
    blocked   = block_override or (context_window - reserve - BLOCK_BUFFER)
    precompute = min(effective - round(effective * PRECOMPUTE_FRAC), threshold)
    state = ("blocked" if tokens >= blocked else
             "compact" if tokens >= threshold else
             "warn"    if tokens >= warn else "ok")
    return Levels(effective, warn, threshold, blocked, precompute, state,
                  pct_left=(threshold - tokens) / threshold)
```

Worked example (200k window, 64k max output): effective 180k; threshold 167k (~83%); warn 147k; precompute 144k; blocked 177k.

### 4.2 Per-turn auto-compaction controller

```python
def before_request(session):
    if compaction_disabled() or auto_disabled(): return
    lv = levels(...)
    if lv.state == "blocked" and larger_window_model_available(session):
        return compact(session, model=larger_model, auto=True)
    if session.consecutive_failures >= 3: return             # circuit breaker
    if prefix_tokens(session) > lv.threshold:
        log("compaction cannot help"); return
    if session.thrash_strikes >= 3:
        notify_user_thrashing(); return
    if classifier_context_overflowed(session):
        compact(session, auto=True)
        inject_priority_reminder("re-issue the dropped tool calls"); return
    if lv.state in ("compact", "blocked") and threshold_enforced():
        if session.precomputed and precompute_still_valid(session):
            swap_in_precomputed(session); return
        try:
            compact(session, auto=True)
            session.consecutive_failures = 0
            track_refill(session)       # increments thrash_strikes if refilled within <3 turns
        except CompactError:
            session.consecutive_failures += 1
```

### 4.3 Full compaction with cache-sharing fork

```python
def compact(session, auto, custom_instructions=None, model=None):
    hook = run_hooks("PreCompact", trigger="auto" if auto else "manual")
    if hook.blocked: raise CompactBlocked
    instructions = join(custom_instructions, hook.extra_instructions)
    request = no_tools_preamble() + summary_prompt(variant()) + instructions + no_tools_closer()

    try:   # warm path: identical system prompt, tools, message prefix ⇒ prompt-cache reads
        text = run_fork(session.cache_safe_params, append_user=request,
                        max_turns=1, write_cache=False, write_transcript=False,
                        can_use_tool=deny_all)
    except Exception:  # cold fallback
        text = run_plain(system=generic_summarizer_role(),
                         messages=strip_media(session.messages) + [request], tools=[])
    text = ptl_retry_if_needed(...)                        # §4.4
    summary = extract_summary_section(text)                # drop private analysis section
    pre = session.tokens
    read_cache_snapshot = session.read_file_state.snapshot(); session.read_file_state.clear()
    attachments = build_reinjection(session, read_cache_snapshot)   # §4.5
    boundary = CompactBoundary(parentUuid=None, logicalParentUuid=session.leaf,
                               compactMetadata=dict(trigger=..., preTokens=pre,
                               preCompactDiscoveredTools=session.discovered_tools, ...))
    hooks_out = run_hooks("SessionStart", source="compact") + run_hooks("PostCompact")
    session.messages = [boundary,
                        continuation_message(summary, transcript_path, auto=auto),
                        *kept_messages, *attachments, *hooks_out]
```

### 4.4 Summary-overflow retry

```python
def ptl_retry(rounds, call):
    for attempt in range(3):
        try: return call(rounds)
        except PromptTooLong as e:
            if e.token_gap: rounds = drop_oldest_until(rounds, freed >= e.token_gap)
            else:           rounds = rounds[ceil(len(rounds) * 0.2):]
            rounds = [truncation_marker()] + rounds     # marks earlier history as cut
    raise CompactError
```

### 4.5 Post-compaction re-injection (deterministic, budgeted)

```python
def build_reinjection(session, read_cache):
    out = []
    files = [f for f in read_cache.most_recent()
             if f not in kept_message_files and not is_memory_file(f) and not is_plan_file(f)]
    total = 0
    for f in files[:5]:
        body = reread_from_disk(f, max_tokens=5_000)
        if total + tokens(body) > 50_000: break
        out.append(file_attachment(f, body)); total += tokens(body)
    skill_total = 0
    for s in session.invoked_skills.newest_first():
        body = truncate(s.body, 5_000, note="truncated; read the skill file for the rest")
        if skill_total + tokens(body) > 25_000: break
        out.append(skill_attachment(s, body)); skill_total += tokens(body)
    if session.plan_file:   out.append(plan_reference(session.plan_file, content=True))
    if session.mode == "plan": out.append(plan_mode_reminder())
    out += background_task_status() + async_shell_status() + mcp_instructions()
    out += deferred_tool_listing() + active_goal()
    return out
# Planning budget for the post-compact window: 20k + 5*5k + 25k = 70k tokens.
```

### 4.6 Reactive compaction

```python
def reactive_compact(messages, token_gap):
    groups = split_into_api_rounds(messages)        # new group at each new assistant message id
    keep = 0; acc = 0
    if token_gap:
        for g in reversed(groups):                  # how many recent rounds can stay
            acc += tokens(g)
            if acc >= token_gap: break
            keep += 1
    head, tail = groups[:len(groups) - keep], groups[len(groups) - keep:]
    try:
        summary = summarize(head, continue_without_questions=True)
    except PromptTooLong:
        summary = summarize(groups[:1]) or None     # last resort: opening round alone
        if summary is None: return head_truncate(messages)
    return [boundary(), continuation(summary),
            verbatim_tail_note(), *flatten(tail)]   # note: tail unseen by summarizer
```

### 4.7 Transcript load / resume / fork

```python
def load(path, at_uuid=None, fork=False):
    rows = read_jsonl(path); meta = fold_metadata(rows)      # last-wins / accumulate
    by_id = {r.uuid: r for r in rows if r.type in TRANSCRIPT_TYPES}
    leaf = (at_uuid or meta.last_prompt_leaf or
            newest(r for r in by_id.values() if not r.isSidechain and reaches_conversation(r)))
    chain, seen = [], set()
    node = by_id.get(leaf)
    while node:
        if node.uuid in seen: report_cycle(); break
        seen.add(node.uuid); chain.append(node)
        node = by_id.get(node.parentUuid)            # stops at boundary (parentUuid None)
    chain.reverse()
    replay_content_replacements(chain, meta.content_replacements)  # byte-identical prefix
    sid = new_session_id() if fork or not adoptable(meta) else meta.session_id
    return Session(sid, chain, meta)
```

Writing: append rows; after each metadata change, re-append current last-wins metadata at the tail.

### 4.8 Transcript GC

```python
def maybe_gc(file):
    if size(file) < 5 * MiB: return
    if not (boundary_followed_by_real_message(file) or appended_since_gc(file) >= file.backstop):
        return
    keep = rows_after_last_boundary(file) | preserved_segment_rows(file) \
         | dangling_parent_targets(file) | latest_last_wins_rows(file)
    saved = rewrite_atomically(file, keep)
    file.backstop = min(file.backstop * 2, 160 * MiB) if saved < 0.10 else 20 * MiB
```

### 4.9 Checkpoints & rewind

```python
def before_edit(session, path):
    if path in session.tracked: return
    name = None
    if exists(path):
        name = f"{hex16(sha256(path))}@v1"
        copy_preserving_mode(path, backup_dir(session) / name)
    session.tracked[path] = Backup(name, version=1, realParentDir=realpath(dirname(path)))

def snapshot(session, message_id):
    snap = {}
    for path, prev in session.tracked.items():
        if unchanged(path, prev):          # mode+size+mtime, then content compare
            snap[path] = prev
        else:
            v = prev.version + 1
            name = f"{hex16(sha256(path))}@v{v}" if exists(path) else None
            if name: copy_preserving_mode(path, backup_dir(session) / name)
            snap[path] = Backup(name, v, realParentDir=realpath(dirname(path)))
    session.snapshots.append(Snapshot(message_id, snap))
    if len(session.snapshots) > 100: evict_oldest_and_delete_unreferenced_backups(session)
    append_row("file-history-snapshot", ...)

def rewind_files(snapshot, dry_run=False):
    stats, skipped = Diffstat(), 0
    for path, b in snapshot.trackedFileBackups.items():
        if is_symlink(path) or nlink(path) > 1 or not is_regular_or_missing(path) \
           or realpath(dirname(path)) != b.realParentDir:
            skipped += 1; continue                       # link-safety
        stats += diff(path, b)
        if not dry_run:
            delete(path) if b.backupFileName is None else restore(b, path)
    return stats, skipped
```

---

## 5. Constants & knobs

| Name | Value / meaning |
|---|---|
| Output reserve cap | 20,000 tokens |
| Auto-compact buffer | 13,000 |
| Warn gap | 20,000 below threshold |
| Block buffer | 3,000 |
| Precompute fraction | 20% of effective |
| Large-turn flag | 200,000 |
| Image token estimate | 2,000 |
| Bytes/token | 4 default; 3 some families; 2 JSON |
| Window env clamp | [100,000, 1,000,000] |
| Failure breaker | 3 consecutive |
| Thrash breaker | refill in <3 turns, 3 times |
| Summary retry | 3 attempts, drop 20% of rounds if gap unknown |
| Re-inject files | 5 files × 5k, 50k total |
| Re-inject skills | 5k each, 25k total |
| Post-compact budget estimate | 70k |
| Idle tool clearing | idle ≥3900 s; keep 5; clear ≥20k; trigger thresholds 3 / 20 tool uses |
| Micro-compaction | keep 5; min savings 20k |
| Context hint | target 75k; enable when ≥20k clearable; retry on 422/424 |
| Idle compaction | 1h TTL; ≥200k tokens (floor 100k); fire at 90% of remaining TTL |
| Picker window | 64 KiB head/tail |
| GC | min file 5 MiB; backstop 20 MiB → doubling to 160 MiB if <10% saved |
| Slug | 200 chars + base-36 hash |
| Checkpoint cap | 100 snapshots |
| Retention | 30 days default, min 1 |
| Env | `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`, `DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`, `CLAUDE_CODE_COLD_COMPACT`, `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`, `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC`, `CLAUDE_CODE_SKIP_PROMPT_HISTORY`, `CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING`, `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING` |
| Settings | `autoCompactEnabled`, per-model auto-compact window, `precomputeCompactionEnabled`, `idleCompaction`, `fileCheckpointingEnabled`, `cleanupPeriodDays`, `desktopSessionCleanupPeriodDays` |
| Flags | `--no-session-persistence`, `--session-id`, `--continue`, `--resume`, `--resume-session-at`, `--fork-session` |

---

## 6. Rationale

- **Ladder ordering**: each cheaper layer reduces how often the expensive, lossy one has to run; summaries are the last resort because they lose detail and cost a model call.
- **Cache-sharing fork**: summarizing with the exact cached prefix turns the cost into mostly cache reads; a different system prompt would force re-reading the whole transcript uncached.
- **Re-injection with budgets**: the summary can omit anything the harness will deterministically restore; telling the summarizer what will survive improves summary quality.
- **Persist-then-clear**: dropping tool output is only safe if the model can fetch it back; logging replacements keeps resumed requests byte-identical for cache hits.
- **TTL-aware scheduling**: a warm cache makes summarization cheap; once the TTL has lapsed, the prefix costs full price anyway, so stale tool uses should be cleared server-side.
- **Uuid DAG**: forks, rewinds, sidechains and parallel tool branches all fall out of parent pointers in an append-only log; metadata at the tail keeps pickers fast.
- **Copy-on-first-touch checkpoints** avoid snapshotting the whole tree; link checks prevent rewinds writing through symlinks/hardlinks to unintended targets.

---

## 7. Harness design takeaways

1. **T1 Layer the pressure relief.** Order mechanisms from cheapest/reversible to most lossy (server clearing → client clearing with disk spill → precomputed summary → threshold summary → reactive summary); each layer reduces firings of the next.
2. **T2 Summarize as a cache-sharing fork.** Append the summarization request to the exact cached prefix (same system prompt and tools, tools denied at runtime) so it costs cache reads; keep a cold fallback.
3. **T3 Tell the summarizer what will survive.** List what the next window will contain so the model keeps pointers rather than re-fetchable content, and back that promise with a deterministic, budgeted re-injection step.
4. **T4 Make clearing lossless.** Spill dropped tool results to files, leave a stub with the path, and log each replacement so resumes reproduce identical bytes and keep cache hits.
5. **T5 Schedule around cache TTL.** Compact while idle shortly before a long TTL expires (large sessions only); after expiry, clear stale tool uses since the prefix is uncached anyway.
6. **T6 Add circuit breakers.** Consecutive-failure, rapid-refill thrash, and prefix-overflow detection, each surfacing an actionable message to the user.
7. **T7 Derive thresholds from the effective window.** Reserve output tokens and a fixed buffer, keep separate warn and blocked levels, and expose env overrides and per-model settings.
8. **T8 Keep recent rounds verbatim on reactive compaction.** Group by API round, summarize only the oldest groups sized from the server's gap, and label the verbatim tail so the agent does not redo finished work.
9. **T9 Store transcripts as an append-only uuid DAG.** Parent pointers give fork/rewind/sidechains/parallel branches; last-wins metadata re-appended at the tail; GC behind boundary rows that record the logical parent and preserved segment.
10. **T10 Checkpoint by copy-on-first-touch plus per-prompt snapshots.** Hashed names with versions, null meaning "did not exist", stat-based dedup, a 100-snapshot cap, link/realpath safety on restore, and an explicit reminder to the model when code and conversation diverge.
11. **T11 Protect user constraints across summaries.** Require a full list of user messages, verbatim carry-over of constraints, and refusal to attribute user-looking text from assistant/tool content to the user; the summary is also an injection boundary.
12. **T12 Put hooks at every lifecycle edge.** Pre-compaction hooks can block or add instructions; session-start (compact) and post-compaction hooks can re-inject project state.

---

## 8. Uncertainties

- Exact membership of the server-clearable ("re-fetchable") tool set is only partly known.
- Precompute swap-validity rules and the sidecar format were not fully traced.
- Which summary prompt variant is active in production is server-configured; the code default is the full structured variant.
- Server semantics of the context-hint rejection (422/424) are not visible client-side.
- Whether todo/task lists have a dedicated re-injection attachment is unconfirmed (probably via task status).
- Host-side context-recovery/recap features were only glimpsed.
