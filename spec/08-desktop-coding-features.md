# 08 — GUI host layer for a coding agent (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

This document specifies a **GUI host** that turns an unmodified command-line coding agent ("the engine") into a desktop coding-session product. The host:

- launches the engine through its SDK (a streaming query API over stdio) rather than reimplementing the agent loop;
- answers every permission request through its own approval UI;
- injects lifecycle hooks that run in the host process;
- exposes its own in-process MCP servers so that GUI features (task list, next-step box, panes, side sessions, PR monitoring) are tools the model calls;
- holds long-lived state (rewind points, worktree leases, CI notification history, child-session notes, warm processes) outside the model's context.

Out of scope: the engine's own loop, compaction and tool internals (files 01–07), and computer use (files 09–10).

---

## 2. Concepts & data model

### 2.1 Architecture

```
  GUI (renderer)                          Host core (main process)                         Engine process
  ──────────────                          ────────────────────────                         ──────────────
  composer + @-mentions ── file index ──► session manager
  approval cards ◄──────────────────────── permission broker ◄── canUseTool callback ◄──── permission ask
  task list / next-step ◄── turn server (update_plan, wrap_up) ◄──────── MCP call ◄─────── model
  panes / split view   ◄── view & window servers
  sidebar sessions     ◄── session server (start_session, spawn_task, …)
                            in-process hooks (pre/post tool, stop, session start,
                            prompt submit, tool batch, worktree create/remove)
  background engines:  CI auto-fix poller · worktree pool · spare-process pre-warm ·
                       title generator · transcript search · usage statistics · asides
```

Three governing ideas (Confidence: observed):
1. **Control by configuration, not by patching.** Every GUI behaviour is one of: an MCP tool the model calls, a hook the host answers, or a context note injected into the conversation.
2. **System prompt = ordered conditional fragments, frozen per session** for prompt-cache stability.
3. **Host is the source of truth.** The model is woken by host-written messages wrapped in envelopes that state their author and authority.

### 2.2 Session start options (record passed to the SDK query)

| Field | Rule |
|---|---|
| `cwd` | session folder, or its worktree path when one exists |
| `model`, `effort` | model id or "default"; effort clamped to what the model supports, and the clamped value written back to the session |
| `thinking` | adaptive when the model supports it |
| `managedSettings` | organisation policy for the spawn target |
| `settings` | session settings + fast-mode flag + auto-mode classifier rules for host tools (§3.4) + lazy-worktree base ref ("head") + denied MCP servers |
| `canUseTool` | the broker callback (§3.3) |
| dialog support | a dialog callback plus the list of dialog kinds the GUI can render |
| surface capabilities | declare "per-task stop" and "rapid follow-up preempts current turn" |
| `enableFileCheckpointing` | true (required for file rewind) |
| `permissionMode` | requested mode, but bypass is downgraded to accept-edits when not permitted or when running as the superuser |
| `settingSources` | user, project, local; user only for launcher-marked worktrees |
| `includePartialMessages` | true (streaming deltas) |
| extra CLI args | thinking display (summarised/omitted); echo user messages back so the GUI can reconcile message UUIDs; project-config root |
| `hooks` | base hooks + session-start note hook + resume-cost hook + activity hooks + turn-server hooks + preview hooks |
| ask-user-question preview | HTML preview format when enabled |
| prompt suggestions | on unless unattended, the task-list box is mounted, or user disabled |
| auth | token provider callbacks so tokens rotate without respawning |
| `additionalDirectories` | folder worktrees and managed dirs |
| `disallowedTools` | scheduled runs lose the ask-user tool; when the host's PR monitor is present the engine's own PR-subscribe tool is removed; unattended runs lose `move_to_cloud` and onboarding/suggestion tools |
| `resume`, `resumeSessionAt`, `forkSession` | used by rewind (§4.3) |
| `systemPrompt` | engine preset + host append (+ snapshot), or fully custom for seeded spawns |

Confidence: observed.

### 2.3 Environment for the engine

Base env is memoised per environment scope; per-session additions follow. Required behaviours (flag names are the implementer's): disable engine micro-compaction; use the system CA store and the login-shell PATH; flush the transcript eagerly; pass the host session id; optionally reap background shells under memory pressure, disable classifier summaries, enable findings reporting, set a preview-classifier floor; disable interactive-only features for unattended runs. Confidence: observed.

### 2.4 Permission request record (host → GUI)

```
PermissionRequest {
  requestId, sessionId, toolName, input (canonicalised), suggestions[],
  decisionReason, description, blockedPath?, engineToolUseId,
  defaultToNo: bool,            # primary button is "No"
  onlyOnCard: bool,             # approvals from notifications/other surfaces ignored
  noAllowOnNotification: bool,
  suppressAlwaysAllowRule: bool,
  scheduledRunApprovalDays?: int
}
Decision = "deny" | "once" | "always" | "scheduled"
```

### 2.5 Turn-server tool schemas (user-visible MCP tools)

`update_plan` — exactly one of three shapes:
- `{items: [{content (≤7 words), status, session?}] (1–12), scope: "first_task"|"continuation"|"new_task"}`
- `{advance: true}` — mark current done, start next
- `{done: [n…], start: m}`

`wrap_up`:
- `recap` (soft ≤300 chars, hard cap 1200), `ask` (≤180), `blocked` (bool),
- `actions` (≤2): either `{action: <app action>}` or `{label (≤24 chars, ≤4 words), message (≤300)}`.
- App action enum: `commit, create_pr, merge, fix_ci, address_comments, resolve_conflicts, verify, archive`.
- Legacy field names are accepted with a migration warning.

Both tools are always loaded (not deferred), annotated read-only, and enabled only for host coding sessions with the task-list box on. Confidence: observed.

### 2.6 Session-lifecycle tools (user-visible MCP tools, `ccd_session` family)

`spawn_task`, `dismiss_task`, `mark_chapter`, `start_session`, `hand_off_to_session`, `list_start_targets`, `move_to_cloud`, `read_widget_context`.
- `spawn_task`: only a suggestion chip; its prompt must be self-contained.
- `start_session`: `initiation ∈ {offer, user_asked, own_initiative}`, `context ∈ {fork, fresh}`; `fresh` requires a `background` string of ≥80 chars. Unaccepted proposals expire after one week.

Other host tool families exposed to the model: view/pane (`ccd_view`), window/split (`ccd_window`), PR (`ccd_pr`), host utilities (`ccd_host`, including `sync_with_base_branch`), settings (`ccd_settings`), sidebar, connectors, directory.

### 2.7 Worktree pool record

```
PooledWorktree { path, repoGroup, branch, leasedTo?: sessionId, pinned: bool,
                 parkedAt, fingerprint: {indexMtime, indexCtime, indexSize, headContents} }
```
Persisted to a JSON store with owner-only permissions (0600). A corrupt store is moved aside and the pool starts empty.

### 2.8 CI watcher state (per PR)

```
PrWatch { prRef, sessionId, headSha, deliveredKeys: set("<headSha>:<checkName>"),
          deliveredComments: set("r<id>" | "i<id>"), failureCount, conflictReminders,
          lastConflictReminderAt, episode?: {startedHead, openedAt} }
```
Delivered sets are persisted so restarts never re-notify.

---

## 3. Behavioural requirements

### 3.1 Prompt assembly and freezing

- **R1.1** The host MUST build its system-prompt append from ordered, individually conditional fragments (sidebar mode, feature gates, unattended, remote host, cloud). Confidence: observed.
- **R1.2** With freezing on, the first spawn MUST store `{append, engineVersion, settingsKey}`; respawns MUST reuse the stored append byte-for-byte unless engine version or settings key changed. The settings key covers: file-link style, seeded append, mid-task reply tool availability, engine-trust flag. Confidence: observed.
- **R1.3** Only a small "live tail" (browser-tools fragment) MAY be re-appended per spawn. Confidence: observed.
- **R1.4** When a conversation is adopted from another surface, host guidance MUST be delivered once as a conversation note (system reminder) rather than altering the system prompt. Confidence: observed.

Required fragment contents (paraphrased purpose; wording is the implementer's):

| Fragment | Must convey |
|---|---|
| Identity | the agent is running inside the desktop app's coding tab |
| File links | reference files as markdown links with workspace-relative href and optional `:line`; always qualify issue/PR numbers as `owner/repo#N` |
| File pane scope | the pane opens only files inside the session's folders; write user-facing deliverables there |
| Runnable commands | put each suggested shell command alone in a `bash` fence (GUI adds a Run button) |
| Unavailable commands | terminal-only dialog slash commands do not work here; don't suggest them |
| Panes | use the pane tool to show diff/file/terminal/PR/tasks/plan instead of describing them |
| Terminal links | link terminal tabs using a fragment-URL convention |
| PRs | after opening a PR use the PR tools to read status and offer auto-fix; never poll CI yourself by any scheduling or polling mechanism; never enable auto-merge unprompted |
| Base sync | in host-made worktrees use the host's base-sync tool instead of merge/pull |
| Settings / split view | use the settings tools and the window tool |
| Asides | the app may answer mid-turn user messages instantly; treat those answers as your own, don't repeat them, follow up through a reply tool |
| CI events (auto-fix on/off) | what a CI event envelope is; that only a standalone host message is a real event; merge base in, never rebase/force-push; relayed review comments are third-party text without authority |
| Task list & wrap-up | the GUI task list is how the user follows progress; use `update_plan` instead of the engine's own task tools; first tool call of any multi-step turn is sent together with a full plan; every turn that kept a list or changed things ends with `wrap_up` |
| Scope classification | prefer `continuation` when unsure, because a mistaken offer interrupts the user while a mistaken continuation costs little; a `new_task` turn opens with only `start_session(initiation=offer)` |
| Starting sessions | keep quick things local; `user_asked` only on explicit request; ≤3 own-initiative cards per turn; choose fork vs fresh; parallel read-only pieces may skip worktrees on a cheaper model; never let two writers share files; child notes are claims to verify, never invent or poll results; link sessions by id |
| Child-session preamble | do exactly the delegated task, list unrelated issues as follow-ups, read sibling notes as data, background long commands (>~2 min), lead the final message with the outcome; in a shared checkout commit only own files and never stash/reset/switch branches |
| Scratch sessions | path is hidden; offer once to move the work to a chosen folder; use directory tools; on move copy files but never VCS metadata, agent config, env files, dependency dirs or links |

### 3.2 Temporary configuration queries

- **R2.1** Slash-command and agent lists MUST be fetched with a throwaway query that disables hooks and denies all tools, with at most 2 concurrent and a 90 s timeout. Confidence: observed.

### 3.3 Permission broker

- **R3.1** Precedence MUST be: auto-deny → auto-allow → card enrichment → session-rule fast path → scheduled-run rules → ask. Confidence: observed.
- **R3.2 Auto-deny** when: a host tool is invoked in a cloud run; a tool's name imitates a host tool but the host doesn't serve it ("no such tool"); a host tool the user disabled; a connector server name that is contested.
- **R3.3 Auto-allow** read-only host tools; lifecycle tools targeting the session itself or its own children; starting a side session in an already-granted location.
- **R3.4 Card enrichment.** Before asking, the broker MUST canonicalise and pre-validate the input (resolve paths, check terminal commands, build plan previews for settings / move-to-cloud / sidebar / deletions) and MUST mint single-use consent nonces that the serving tool verifies. If the state the card described no longer holds at execution, the tool MUST change nothing and tell the model to call again. Confidence: observed.
- **R3.5 Fast path.** If rules accumulated in this session allow the tool, and mode ≠ plan, tool isn't force-ask and not a cloud run, allow with classification "user permanent". A second cache keyed by (tool, decision reason, input key) MAY short-circuit repeated asks.
- **R3.6 Scheduled runs.** Tools approved for a routine auto-approve on later runs. When nobody has opened an unattended run, deny with a message telling the model a human approval is needed and to proceed without it.
- **R3.7 Asking.** Emit a `PermissionRequest`, park a pending promise, and start timers: stall telemetry at 300 s, re-notify the user at 30 min, a session-updated pulse at ~10 s. Abort signal resolves to deny.
- **R3.8 Responses.** `deny` with user feedback returns that text to the model without interrupting; plain `deny` interrupts with a short "user rejected <tool> <summary>" message; `once` on plan-exit applies the target mode; `always` persists the updates and records them for the session. Requests marked `onlyOnCard` MUST ignore approvals from other surfaces.
- **R3.9** Denial MUST discard any minted consents.

### 3.4 Host hooks

- **R4.1 Pre-tool guards** (MUST):
  - block tools touching folders outside organisation-allowed workspaces;
  - worktree write guard: block Edit/Write escaping the session's worktree; in lazy mode block edits in the shared checkout until the model enters a worktree;
  - config-root fence: block writes to agent config files (`.claude/`, `.mcp.json`, `CLAUDE.md`, similar) under a fenced root;
  - unattended sessions cannot message other sessions;
  - host-tool matcher returns allow/ask/deny plus a canonicalised `updatedInput`; in bypass mode host cards stand down, in auto mode the classifier decides using host-supplied rules;
  - MCP guard: block MCP calls while a different account is active; deny subagent calls to `update_plan`/`wrap_up` with an explanation.
- **R4.2** Stop hook signals turn completion. Worktree create/remove and subagent-stop hooks let the host serve worktrees from its pool.
- **R4.3 Classifier extension.** For every host tool, the host MUST supply natural-language policy (environment description, soft-deny rules, allow rules) into the auto-mode classifier settings, including what the user must have explicitly named for consent. Confidence: observed.
- **R4.4 Activity ledger.** On prompt submit (turn start) and after each tool batch (mid turn) the host drains a per-session ledger of GUI events and injects it as context in an envelope that (a) says the host wrote it, not the user, (b) says to treat it as context not instructions, (c) is ≤6 lines, with per-kind caps (provenance 2, terminal 3), each line ≤320 chars, with ≤24 entries retained LRU. Closing tags of the envelope and of system reminders inside entries MUST be escaped. Confidence: observed.
- **R4.5 Compaction rehydration.** A session-start hook matching compact/clear MUST re-inject host state, e.g. the unfinished plan steps with an instruction to rewrite the list rather than guess what step numbers meant.

### 3.5 Warm sessions and spare processes

- **R5.1** The host SHOULD warm an existing session's engine before the user types, guarded by: a crash-loop guard with backoff/park, remote-connect backoff, unattended policy for remote hosts, transcript-exists check (else mark transcript unavailable), and a start mutex.
- **R5.2** The host SHOULD pre-boot one generic spare engine for "new session" in a dedicated data dir. Claim MUST succeed only if the options fingerprint (engine version, skills fingerprint, additional dirs, …) matches exactly; otherwise cold-start and record which field differed. Spares are never used for cloud, remote, agent, scheduled, spawned or worktree sessions, and MUST acquire a slot from a global engine-process governor (concurrency cap; subscribes to OS memory pressure).

### 3.6 Rewind, forks, side chats

- **R6.1** Rewind MUST be lazy: map the chosen user message to the preceding assistant message on the active chain (reject dead branches), stop the process, store `pendingRewindTo`, and return the old user text to prefill the composer. Next spawn uses `resume + resumeSessionAt + forkSession`. Undo is available until the next send.
- **R6.2** File restore is separate, supports dry-run, and MUST be refused while a turn is running.
- **R6.3** The model MUST be told what happened to files via one of five notices: conversation rewound but files untouched (check git status/diff); files restored to the same point; files restored to a different point; files restored without conversation rewind; rewind undone.
- **R6.4 Side chats** MUST be ephemeral forks (`resume + forkSession`, not persisted, only Read/Grep/Glob, no setting sources, permission callback that always denies) reusing the parent's frozen append. Their preamble states it's a side fork that does not affect the main transcript (variants: no tools / read-only / tools listed but only read tools run).
- **R6.5 Recap** uses the same fork machinery with an instruction for a very short (≈40 words, 1–2 sentences, no markdown) "welcome back" summary: goal, current task, next action. Token-cap windows are checked before every helper fork.
- **R6.6 User forks** add a prompt section saying the session was forked; a lightweight "by the way" fork reports nothing back automatically but may message the origin session.

### 3.7 Turn server

- **R7.1** `update_plan` with `items` but no `scope` MUST fail with a teaching error telling the model to resend the same items with scope. Each successful result MUST list the app actions currently available.
- **R7.2** `wrap_up` MUST refuse unavailable app actions with an explanation; MUST drop custom labels/messages that contain `@`, start with `/` or `!`, or contain invisible Unicode (a button click must not trigger mentions or commands). Its result MUST tell the model the closing message should add only what the recap lacks, ≈120 words max. The recap's first sentence becomes the sidebar status line.

### 3.8 Worktrees

- **R8.1 Lazy worktree.** Session starts in the user's checkout; the prompt says edits require entering a worktree first and that this is enforced. On the engine's worktree-create hook the host provides a tree: prepared → pooled → freshly created. After a process restart the model is told it had moved into a given worktree earlier.
- **R8.2 Pool.** Worktrees of archived sessions are parked with a fingerprint and re-leased to new sessions of the same repo only if the fingerprint still matches. Reap beyond a per-repo-group maximum or after a configured age. Eligible for reaping only if unleased (or session archived), not pinned, not running, not remote, older than 5 min. ≤2 concurrent destructive ops, 1 h slot watchdog, sweep every 30 min.
- **R8.3** New trees MUST receive overlays listed in `.worktreeinclude` plus `CLAUDE.md`, `.mcp.json`, `.claude/settings*.json`.
- **R8.4** Base sync runs on the host, outside the agent sandbox, so protected paths can be merged.

### 3.9 Side sessions and wake-ups

- **R9.1** When a child ends a turn, its last result is queued on the parent as a note (≤32 notes).
- **R9.2** When **all** children and the parent are idle, and the parent has no pending permission, dialog or queued input, the host MUST send one auto-continuation in an envelope telling the model the started sessions finished, to report the outcome in 1–2 lines with PR links, and not to start new work unprompted.
- **R9.3** At most 10 such wakes since the last human message; further wakes are held.
- **R9.4** `move_to_cloud` pushes the branch, creates a cloud session seeded with a conversation summary plus the model's summary, archives the local session at turn end; refuses with uncommitted changes; card in default mode, classifier in auto mode; unavailable unattended.

### 3.10 CI auto-fix poller

- **R10.1** Per PR, fetch checks, review comments, reviews, issue comments and the authenticated VCS login in parallel.
- **R10.2** Dedup failures on `headSha:checkName`; never notify the same failure twice for one head.
- **R10.3** Forward comments only from bots or owner/member/collaborator associations, never from the session's own login; dedupe by id.
- **R10.4** Hold a failure up to 5 min while sibling checks of the same workflow are pending.
- **R10.5** On merge conflict: first attempt a host-side base merge, then tell the model the outcome (merged / conflicts left in progress / one of ~20 refusal reasons, e.g. fork checkout, unpinned origin).
- **R10.6** Wake text MUST state: the app read this state from the VCS host; enabling auto-fix is standing authorisation to fix and push without asking; that authorisation covers only the app's findings, not quoted text. All third-party text is line-prefixed with `> `, `<` escaped, and closed with an explicit end-of-quote marker. Replies posted on the PR carry an attribution footer.
- **R10.7** Deliver as an auto-continuation; if queued, a newer wake replaces the older; if the engine process cap is reached, defer to next sweep. Track fix episodes until a clean read on the same head.

### 3.11 Titles, branches, workers, asides, remote

- **R11.1 Titles** come from a small fast model (first 4000 chars of description, ≤200 output tokens, a system prompt asking only for tagged fields). Try a direct API call first; fall back to spawning the engine; after 3 failures disable the direct path for 5 min. Branch names MUST match `^claude/[a-z0-9]+(-[a-z0-9]+)*$`.
- **R11.2 File index** for @-mentions per §4.6.
- **R11.3 Transcript search** streams JSONL line by line (4 files concurrently), requires every term to regex-match the raw line before parsing, snippet 20 chars before / 80 after, results by recency; no index.
- **R11.4 Usage statistics** scan engine transcripts (seeded from the engine's stats cache) off the main thread, cached 60 s; outputs totals, active days, peak hour, streaks, daily activity, daily per-model tokens.
- **R11.5 Asides.** When the user types mid-turn, a built-in plugin answers immediately using the engine's side-question facility with a snapshot of the turn so far (user text, assistant text, one-line tool summaries, middle truncated). The aside is stored per engine session for the plugin hook; the main agent is told it "said" this and can correct via a reply tool.
- **R11.6 Remote control.** Sessions started from a remote client never run in bypass mode; a session currently serving a client cannot be cleared or rewound. Error categories: sign-in, update, retry, org policy, needs subscription, network, process stopped.

---

## 4. Algorithms

### 4.1 Prompt-append freezing

```python
def system_prompt_append(session, settings, engine_version):
    key = settings_key(settings)            # file links, seeded append, reply tool, trust
    frozen = session.frozen_append
    if frozen and frozen.version == engine_version and frozen.key == key:
        base = frozen.text
    else:
        base = "\n\n".join(f.render() for f in FRAGMENTS if f.enabled(session, settings))
        session.frozen_append = Frozen(base, engine_version, key)
        log("rebuilt", reason="version_changed" if frozen and frozen.version != engine_version
                          else "settings_changed" if frozen else "first")
    return base + live_tail(session)        # only volatile fragment
```

### 4.2 Permission broker with consent nonce

```python
def can_use_tool(session, tool, raw_input, ctx):
    if v := auto_deny(session, tool):          return deny(v)
    if auto_allow(session, tool, raw_input):   return allow(raw_input)
    shown_input, preview = canonicalise_and_validate(tool, raw_input)   # may reject early
    if session.rules.allow(tool, shown_input) and session.mode != "plan" \
       and not force_ask(tool) and not session.is_cloud:
        return allow(shown_input, classification="user_permanent")
    if session.is_scheduled:
        if routine_preapproved(session, tool): return allow(shown_input)
        if not session.opened_by_human:        return deny(needs_human_msg(tool))
    req = PermissionRequest(new_id(), tool, shown_input, preview, ...)
    gui.emit(req)
    decision = await wait_with_timers(req, stall=300, renotify=1800, pulse=10.25,
                                      abort=ctx.signal)
    match decision.kind:
        case "deny":
            drop_consents(req)
            return deny(decision.feedback, interrupt=decision.feedback is None)
        case "once" | "always" | "scheduled":
            if decision.surface != "card" and req.onlyOnCard: return await_again(req)
            nonce = mint_nonce(tool, digest(shown_input))       # single use
            if decision.kind == "always": persist_rules(session, decision.updates)
            return allow({**shown_input, "_consent": nonce})

# inside the host tool server
def serve(tool, args):
    if not consume_nonce(args.pop("_consent"), tool, digest(args)):
        return error("approval no longer matches; nothing changed; call again")
    if not preconditions_still_hold(tool, args):
        return error("state shown on the card changed; nothing changed; call again")
    return execute(tool, args)
```

### 4.3 Lazy rewind

```python
def rewind(session, user_msg_uuid):
    chain = active_chain(session.transcript)
    if user_msg_uuid not in chain: raise Rejected("dead branch")
    target = chain.prev_assistant(user_msg_uuid)
    stop_engine(session)
    session.undo = session.pending_rewind; session.pending_rewind = target
    return {"prefill": text_of(user_msg_uuid)}

def spawn_options(session):
    opts = base_options(session)
    if session.pending_rewind:
        opts |= dict(resume=session.engine_id, resumeSessionAt=session.pending_rewind,
                     forkSession=True)
    return opts

def on_send(session):                       # undo window ends here
    session.undo = None
    note = file_notice(session.rewind_state)   # one of five notices
    if note: inject_note(session, note)
```

### 4.4 Spare pre-warm with fingerprint

```python
def prewarm():
    if failures_in_last(600) >= 3 or not governor.try_slot(): return
    opts = generic_new_session_options()
    spare = Spare(engine=boot(opts, timeout=30), key=fingerprint(opts), born=now())

def claim(requested_opts, session_kind):
    if session_kind in {"cloud","remote","agent","scheduled","spawned","worktree"} \
       or not spare or now() - spare.born > 600:
        return cold_start(requested_opts)
    diff = first_diff(spare.key, fingerprint(requested_opts))
    if diff: log("spare_miss", field=diff); return cold_start(requested_opts)
    return spare.take(timeout=30)
```

### 4.5 CI auto-fix poller

```python
def sweep_loop():
    sleep(15)
    while True:
        prs = due_prs()                                   # backoff & closed-PR cadence
        parallel_map(check_pr, prs, limit=5)
        sleep(300 if window_unfocused_or_idle() else 60)

def check_pr(w):
    checks, rcomments, reviews, icomments, me = gather(...)  # parallel
    if mergeability(w) == "UNKNOWN": sleep(10); re-read
    findings = []
    for c in failed(checks):
        k = f"{w.head}:{c.name[:120]}"
        if k in w.delivered: continue
        if siblings_pending(c) and age(c) < 300: continue  # hold
        findings.append(c); w.delivered.add(k)
    for cm in (rcomments + reviews + icomments)[:20]:
        cid = ("r" if cm.is_review else "i") + str(cm.id)
        if cid in w.delivered_comments or cm.author == me: continue
        if not (cm.is_bot or cm.association in {"OWNER","MEMBER","COLLABORATOR"}): continue
        findings.append(quote(cm.body[:1200])); w.delivered_comments.add(cid)
    if conflicted(w) and conflict_reminder_due(w):     # 30 min * 2^n, ≤3
        findings.append(explain(host_merge_base(w)))
    if findings:
        persist(w)
        deliver_wake(w.session, render_wake(w.prRef, findings))

def deliver_wake(session, msg):
    if engine_cap_reached(): defer_to_next_sweep(session, msg); return
    if session.queue.has_wake(): session.queue.replace_wake(msg)
    else: session.send(msg, origin="auto-continuation", initiator="ci_poller")

def quote(text):
    return "\n".join("> " + l.replace("<", "&lt;") for l in text.splitlines()) \
           + "\n(end of quoted text)"
```
Backoff on fetch failure per PR: skip `min(2**failures, 16)` ticks; closed PRs checked every 30 ticks.

### 4.6 @-mention file index and scorer

```python
def list_files(repo):
    try: return git_ls_files(repo, recurse_submodules=True, timeout=10, hard=15, buf=64<<20)
    except: return bfs(repo, depth=4, max_entries=2000, max_dirs=200, deadline=10,
                       skip={"node_modules",".git","dist","build",".venv","target","vendor"})

def score(query, path):
    if letter_mask(query) & ~letter_mask(path): return None   # 26-bit prefilter
    best = max(subseq_score(query, path), subseq_score(query, basename(path)))
    if best is None: return None
    best += max(0, 32 - len(path)/4)
    if "test" in path.lower(): best /= 1.05
    return best

def subseq_score(q, s):                      # greedy, case-insensitive
    total, last = 0, -1
    for ch in q:
        i = s.lower().find(ch, last + 1)
        if i < 0: return None
        total += 16
        if i == 0: total += 8
        elif s[i-1] in "/\\-_. ": total += 8
        elif s[i].isupper() and s[i-1].islower(): total += 6
        if i == last + 1: total += 4
        else: total -= 3 + (i - last - 1)
        last = i
    return total
```
Keep top-k by binary insertion; empty query returns cached top-level dirs; the listing carries a content hash so the GUI refetches only on change. Content search uses ripgrep JSON output, ≤6 matches per file, git-grep fallback, 200-char previews.


---

## 5. Constants & knobs

| Name | Value |
|---|---|
| Temp-query concurrency / timeout | 2 / 90 s |
| Permission stall telemetry / re-notify / pulse | 300 s / 30 min / ~10.25 s |
| Activity envelope | ≤6 lines; provenance ≤2, terminal ≤3; ≤320 chars/line; ≤24 retained |
| Spare TTL / boot timeout / claim timeout | 10 min / 30 s / 30 s |
| Spare failure budget | 3 per 10 min |
| `update_plan` items / words per item | 1–12 / ≤7 |
| `wrap_up` recap / hard cap / ask / actions / label / message | 300 / 1200 / 180 / 2 / 24 chars & 4 words / 300 |
| Closing-message guidance | ≈120 words |
| `start_session` fresh background | ≥80 chars; proposals expire 7 days |
| Own-initiative session cards per turn | ≤3 |
| Child notes queued | ≤32 |
| Autonomous wakes without human message | 10 |
| Worktree pool: min age / destructive concurrency / slot watchdog / sweep | 5 min / 2 / 1 h / 30 min |
| CI sweep / unfocused sweep / first sweep | 60 s / 300 s / 15 s |
| CI parallel PRs / backoff cap / closed-PR cadence | 5 / 16 ticks / 30 ticks |
| Mergeability settle | 10 s |
| Conflict reminders | 30 min × 2^n, ≤3 |
| Sibling-pending hold | ≤5 min |
| Comments per wake / body cap / check-name cap | 20 / 1200 / 120 chars |
| Title gen input / output / failure cooldown | 4000 chars / 200 tokens / 3 failures → 5 min |
| File index git timeout / hard / buffer / parallel repos | 10 s / 15 s / 64 MB / 4 |
| BFS fallback | depth 4, 2000 entries, 200 dirs, 10 s |
| Scorer weights | +16 char, +8 boundary, +6 camel, +4 consecutive, +8 start, −(3+gap), short bonus max(0,32−len/4), test ×1/1.05 |
| Content search | ≤6 matches/file, 200-char preview |
| Transcript search | 4 files concurrently; snippet 20 before / 80 after |
| Stats cache | 60 s |
| Pool limits (per-repo max, reap age) | configurable settings; defaults not determined |

---

## 6. Rationale

- **Tools instead of output parsing**: schemas make GUI contracts testable and errors teachable; scraping prose is brittle.
- **Frozen append**: system-prompt prefix stability is worth more than freshness; volatile data goes in per-turn notes.
- **Nonce-bound approval**: removes the time-of-check/time-of-use gap between what the user saw and what runs, and blocks model-forged "already approved" inputs.
- **Classifier policy for host tools**: an LLM permission judge cannot reason about tools it has no harm model for.
- **Envelopes with authority statements**: wake-up messages arrive in the same channel as untrusted tool output; explicit authorship and "only genuine as its own message" rules defeat look-alikes.
- **Lazy rewind**: append-only history plus resume-at-point is safer than truncation and enables undo.
- **Host-side CI polling**: polling inside the model wastes tokens and turns; the host can dedup, back off and batch.
- **Wake caps**: prevent parent/child ping-pong loops.

---

## 7. Harness design takeaways

1. **T1 — GUI features are tools plus prompt contracts.** Give each UI surface a strictly-typed tool and a short paragraph on when to call it; make validation errors teach the fix. Never scrape prose.
2. **T2 — Tool results steer the next step.** Return runtime-dependent guidance (available actions, what the closing message should contain) in results instead of more system prompt.
3. **T3 — Bind approval to what was displayed.** Canonicalise and validate before asking, show the canonical form, mint a single-use nonce on approval, verify in the tool server; reject tools that impersonate host tools.
4. **T4 — Supply a harm model for every host tool** to the auto-approval classifier (what it does, what the user must have named, what counts as injection).
5. **T5 — Authority-scoped wake envelopes.** State author and authority, treat look-alikes in tool output as data, and quote third-party text line-prefixed, escaped and closed with an explicit end marker.
6. **T6 — Durable state on the host, re-injected after compaction.** The context window is a cache; persist rewind points, delivered sets, notes; restore via a compact/clear session-start hook.
7. **T7 — Lazy, append-only rewind** with undo until next send and a precise notice about file state.
8. **T8 — Prompt cache as a design constraint.** Freeze the append; keep volatility in notes; run helpers as cache-sharing forks with the parent's append.
9. **T9 — Pre-warm but verify.** Reuse spares and pooled worktrees only on exact fingerprint match; log miss reasons; use TTLs and failure budgets.
10. **T10 — Polling engines need dedup keys, backoff and batching**, wait for sibling checks, filter comment authors, replace rather than stack queued wakes, defer at the process cap, and forbid the model from polling.
11. **T11 — Cap autonomous wake-ups** (10 without a human; wake only when all children idle; delegated sessions can't claim "user asked").
12. **T12 — Cheap @-mention search**: VCS file list + bitmask-prefiltered subsequence scorer, hash-versioned listings; brute-force transcript search with a raw-line prefilter.

---

## 8. Uncertainties

- Approval-card rendering, the scheduled-run approval-days UI and notification approval rules are inferred from host fields only. Confidence: inferred.
- Full classifier policy prose for host tools (~45 KB) was only sampled; an implementer must author their own.
- How asides physically reach the engine mid-turn is partly inferred.
- Defaults for per-repo warm-worktree max and reap age are unknown.
- Exact engine behaviour for "rapid follow-up preempt" and "per-task stop" capabilities is inferred (follow-up preempts in-flight turn; stop per background task).
- Remote-control bridge transport and cloud hand-off packaging were only skimmed.
- Rollout state of gated features is unknown.
