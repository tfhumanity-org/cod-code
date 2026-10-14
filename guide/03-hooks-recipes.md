# 03 — Hooks: cheat-sheet and recipes

> Behaviour described here is as of CLI 2.1.295 and may change. Citations point to the functional spec: `[04 R33]` = requirement, `[04 §2.9]` = data-model section, `[04#T9]` = takeaway, `[Δ n]` = item n of `CHANGES-2.1.236-to-2.1.295.md`. "(inferred)" means the spec marks the point as inferred, or it is my reading of a gap.

Hooks are the only way to make something *always* happen. Instructions are suggestions the model weighs. A hook is a program the harness runs, whether the model remembers or not. Use them for invariants (never touch X, always format Y, don't stop while tests are red) and for feeding fresh state into context. Keep them fast, narrow and predictable. A slow or flaky hook taxes every single turn.

> **TL;DR**
> - Exit codes: **0** means success (stdout parsed as JSON, or used as context for SessionStart/UserPromptSubmit/Setup). **2** means block, and stderr goes to the model. **Anything else** is non-blocking, and stderr is shown only to you [04 R33].
> - Hooks **fail open** by default. A crash, timeout or bad JSON doesn't block anything unless you set `onFailure: "block"` or `timeoutFailsClosed` [04 R33, R36].
> - Narrow every hook with `matcher` (event field) and `if` (permission-rule syntax, e.g. `Bash(git push:*)`) so it only spawns when relevant [04 R31, R32].
> - A hook "allow" **cannot** override deny rules, ask rules or safety checks, and in auto mode it still goes to the classifier. Hooks can only tighten [04 R35] [Δ 34].
> - Command hooks default to a **600 s** timeout, so set `timeout` yourself. Hooks run **outside the sandbox** with your full privileges [04 R36, R39].

---

## 1. Configuration shape

```json
{
  "hooks": {
    "<EventName>": [
      { "matcher": "<pattern>", "hooks": [ { "type": "command", "command": "…", "timeout": 10 } ] }
    ]
  }
}
```

Fields available on every hook type: `type`, `if`, `timeout` (seconds), `statusMessage`, `once` [04 §2.6]. Then per type:

| `type` | Extra fields | Default timeout |
|---|---|---|
| `command` | `command`, `args` (exec form, no shell), `shell` (`bash`/`powershell`), `async`, `asyncRewake`, `onFailure` (`continue`/`block`), `timeoutFailsClosed` | 600 s |
| `prompt` | `prompt` (with `$ARGUMENTS` substituted), `model`, `continueOnBlock` | 30 s |
| `agent` | `prompt`, `model`. Runs at most 50 turns, in `dontAsk` mode, and may only read the transcript | 60 s |
| `http` | `url`, `headers` (`$VAR` allowed only for names listed in `allowedEnvVars`), `allowedEnvVars` | 600 s |
| `mcp_tool` | `server`, `tool`, `input` (supports `${tool_input.<field>}`) | 600 s |

Sources: [04 §2.6, R36]. `SessionEnd` hooks get only **1.5 s** by default, extendable to 60 s with `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` [04 R36].

**Matchers** [04 R31]. An empty matcher, `*` or `.*` matches everything. A pattern made only of identifier characters and `|` (for example `Edit|Write`) is split and compared exactly. Anything else is treated as a regex. **An invalid regex never matches** and is only logged. What the matcher is compared against depends on the event: tool name, session-start source, compaction trigger, notification type, agent type, MCP server name, load reason, file basename, and so on.

**`if`** uses permission-rule syntax with the same matcher as permission rules [04 R32]. `"if": "Bash(git push:*)"` means the process isn't even spawned for other shell calls. That's the main tool for keeping hooks cheap [04 §6].

**Environment.** `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` are expanded in exec-form `args`. `CLAUDE_EFFORT` is exported. `CLAUDE_ENV_FILE` is provided to SessionStart, CwdChanged and FileChanged [04 R38]. The examples below use `"$CLAUDE_PROJECT_DIR"` in shell-form `command` strings. That relies on the variable being present in the hook's environment, which is likely but not spelled out in the spec (inferred). If in doubt, use exec form.

## 2. Events cheat-sheet (33)

Grouped by when they fire [04 §2.7]:

| Group | Events | Typical use |
|---|---|---|
| **Tool lifecycle** | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch` | Guard, rewrite input, format, add context |
| **Permissions** | `PermissionRequest`, `PermissionDenied` | Answer prompts programmatically (headless); react to denials |
| **Prompt** | `UserPromptSubmit`, `UserPromptExpansion` | Inject per-prompt context; block prompts |
| **Session** | `Setup`, `SessionStart`, `SessionEnd`, `ConfigChange`, `InstructionsLoaded` | Load state, env vars, cleanup |
| **Turn end** | `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop` | Verifiers, "keep going" loops |
| **Context** | `PreCompact`, `PostCompact` | Steer or block compaction |
| **Model** | `PreModelSwitch`, `PostModelSwitch` (new in 2.1.295 [Δ 37]) | Veto or log model changes |
| **Workspace** | `CwdChanged`, `FileChanged`, `DirectoryAdded`, `WorktreeCreate`, `WorktreeRemove` | React to the environment; custom worktree provisioning |
| **Tasks & teams** | `TaskCreated`, `TaskCompleted`, `TeammateIdle` | Coordination, notifications |
| **MCP** | `Elicitation`, `ElicitationResult` | Answer server questions automatically |
| **UI** | `Notification`, `MessageDisplay` | Desktop alerts; rewrite displayed text |

**Common stdin** (JSON): `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and optionally `prompt_id`, `permission_mode`, `agent_id`, `agent_type`, `effort.level` [04 §2.8]. PreToolUse adds `tool_name`, `tool_input`, `tool_use_id` and `mcp_server: {name, source}`. PostToolUse adds `tool_response` and `duration_ms` [04 §2.8]. To identify an MCP server, **trust `mcp_server.source`, not the tool-name prefix** [04 §2.8]. Tool inputs: Edit and Write carry `tool_input.file_path`, and Bash carries `tool_input.command` [03 §2.2].

## 3. Output contract

**Exit codes** [04 R33]:

| Exit | Meaning |
|---|---|
| `0` | Success. stdout is parsed as JSON if it can be. For `UserPromptSubmit`, `SessionStart` and `Setup`, plain stdout becomes model context |
| `2` | **Block.** stderr goes to the model. `Stop`/`SubagentStop`: the model is told to keep going. `UserPromptSubmit`: the prompt is erased. `PreCompact`: compaction is blocked |
| other | Non-blocking. stderr is shown to you only |

**JSON on stdout** [04 §2.9]. Top-level fields: `continue`, `suppressOutput`, `stopReason`, `decision` (`approve`/`block`), `systemMessage`, `reason`, `terminalSequence`, `hookSpecificOutput`. Or return `{ "async": true, "asyncTimeout": … }` to go background. Notable `hookSpecificOutput` fields by event:

- **PreToolUse:** `permissionDecision` (`allow`/`deny`/`ask`/`defer`), `permissionDecisionReason`, `updatedInput`, `additionalContext`.
- **PostToolUse:** `additionalContext`, `classifierContext` (passed to the auto-mode judge as host-asserted intent, capped at 2000 UTF-16 units across hooks [04 R40]), `updatedToolOutput`, `updatedMCPToolOutput`.
- **SessionStart:** `additionalContext`, `initialUserMessage`, `sessionTitle`, `watchPaths`, `reloadSkills`.
- **PermissionRequest:** `decision: {behavior: "allow", updatedInput?, updatedPermissions?}` or `{behavior: "deny", message?, interrupt?}`.
- **PermissionDenied:** `retry`. **CwdChanged/FileChanged:** `watchPaths`. **WorktreeCreate:** `worktreePath`. **MessageDisplay:** `displayContent`.

**Merging** [04 R34, §4.7]. All hooks for an event run **in parallel on the same original input**. Command hooks are de-duplicated by command string. The first `deny` short-circuits. `ask` beats `allow`. For `updatedInput`, **last writer wins**. Hook output reaches the model wrapped as a system reminder [02 R30].

## 4. Failure modes

| Situation | Default | Fail-closed option |
|---|---|---|
| Non-zero, non-2 exit | Non-blocking, stderr shown to you | `onFailure: "block"` turns it into exit-2 semantics on events that can block [04 R33] |
| Spawn failure | Treated as exit 1 → non-blocking | `onFailure: "block"` |
| Invalid JSON on exit 0 | Ignored | `onFailure: "block"` (when stdout is non-empty) |
| Timeout | Output discarded, action proceeds | `timeoutFailsClosed` blocks PreToolUse/UserPromptSubmit [04 R36] |

Fail-closed per hook is new since 2.1.236 [Δ 37]. In a restricted session mode, broken security config (settings or guard hooks that fail to load) blocks tool calls entirely [Δ 36]. Global switches: `disableAllHooks`, `allowManagedHooksOnly`, `allowedHttpHookUrls` [04 R39].

**Rule of thumb:** guard hooks protecting something important get `onFailure: "block"`. Convenience hooks (formatting, notifications) stay fail-open, so a broken formatter never stops your work.

---

## 5. Recipes

### R1. Block edits to protected paths, with a useful message

Start with a deny rule. It's cheaper and can't fail: `"deny": ["Edit(generated/**)"]` (see chapter 02). Use a hook when you want to tell Claude **what to do instead**, since exit-2 stderr reaches the model:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Edit|Write|NotebookEdit", "hooks": [ {
  "type": "command", "timeout": 5, "onFailure": "block",
  "command": "f=$(jq -r '.tool_input.file_path // empty'); case \"$f\" in */generated/*) echo \"$f is generated. Edit schema/*.yaml and run 'make gen' instead.\" >&2; exit 2;; esac; exit 0"
} ] } ] } }
```

Why both: a rule match is deterministic and auditable. The hook adds the redirect. `onFailure: "block"` means a missing `jq` blocks the edit rather than silently letting it through.

### R2. Auto-format after each edit

```json
{ "hooks": { "PostToolUse": [ { "matcher": "Edit|Write", "hooks": [ {
  "type": "command", "timeout": 20,
  "command": "f=$(jq -r '.tool_input.file_path // empty'); case \"$f\" in *.ts|*.tsx|*.js) npx prettier --write \"$f\" >/dev/null 2>&1;; *.py) ruff format \"$f\" >/dev/null 2>&1;; esac; exit 0"
} ] } ] } }
```

The trade-off: the formatter changes the file behind Claude's back. The next Edit to that file still works if Claude's `old_string` matches uniquely and the path is auto-allowed. Otherwise it fails and asks for a fresh Read [03 R19]. Claude may also get a diff notice for the changed file [02 R32]. Both cost tokens. On large files, consider formatting once at `Stop` instead (R3), or let Claude run the formatter itself. The harness notices formatter commands and flags which files to re-read [03 R22].

### R3. Don't let Claude stop while tests are red (command verifier)

`Stop` with exit 2 tells the model to keep going [04 R33]. Without a guard, a test that can never pass loops forever, so add a cap keyed on `session_id`:

```bash
#!/usr/bin/env bash
# .claude/hooks/stop-tests.sh
in=$(cat); sid=$(jq -r .session_id <<<"$in")
cd "$CLAUDE_PROJECT_DIR" || exit 0
git diff --quiet && git diff --cached --quiet && exit 0      # nothing changed: allow stop
c="${TMPDIR:-/tmp}/claude-stop-$sid"; n=$(cat "$c" 2>/dev/null || echo 0)
[ "$n" -ge 3 ] && { rm -f "$c"; exit 0; }                     # give up after 3 nudges
if out=$(make test-fast 2>&1); then rm -f "$c"; exit 0; fi
echo $((n+1)) > "$c"
{ echo "Tests are failing. Fix them before finishing:"; tail -40 <<<"$out"; } >&2
exit 2
```

```json
{ "hooks": { "Stop": [ { "hooks": [ { "type": "command", "timeout": 300,
  "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/stop-tests.sh" } ] } ] } }
```

Keep the test target *fast*. This runs at the end of every turn that changed files.

### R4. Stop verifier with a prompt-type hook

When "done" can't be checked by a script ("did it actually run the tests after the last change?"), use an LLM judge. Prompt hooks return structured `{ok, reason, impossible?}` with thinking off. For Stop-type checks the evaluator receives the transcript and must cite evidence. `impossible: true` lets the loop end when the goal can't be reached [04 R37] [04#T9].

```json
{ "hooks": { "Stop": [ { "hooks": [ { "type": "prompt", "timeout": 30,
  "prompt": "Decide whether the assistant may stop. Require evidence in the transcript that, after its last code edit, it ran the project's test command and the run passed. If no code was edited, ok. If tests cannot run in this environment, mark impossible. Context: $ARGUMENTS"
} ] } ] } }
```

It costs a small model call per stop, and transcripts that are too long are retried with half the transcript [04 R37]. Use R3 when a script can decide. Use R4 when judgement is needed.

### R5. Inject project state at start, and again after every compaction

SessionStart plain stdout becomes context [04 R33]. It fires with source `compact` after each compaction [05 R21], and hosts use `compact`/`clear` matchers to re-hydrate state [08 R4.5]. Keep the output short, because every byte is context.

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "timeout": 10,
  "command": "cd \"$CLAUDE_PROJECT_DIR\" && echo \"Branch: $(git branch --show-current)\" && echo 'Changed files:' && git status --short | head -30 && [ -f NOTES.md ] && head -40 NOTES.md; exit 0"
} ] } ] } }
```

Want it only after compaction or `/clear`? Set `"matcher": "compact|clear"`. Treat a running `NOTES.md` as the durable "where we are" file. This pairs with the advice in chapter 01 to keep standing state in files, not chat.

### R6. Persist environment variables for every shell call

There is no persistent shell. Each command starts fresh from a snapshot. The working directory persists but shell variables don't [03 R38, R39], so `export` in one Bash call is gone by the next. Exports written to `CLAUDE_ENV_FILE` during SessionStart persist into later shell calls [04 R38]:

```json
{ "hooks": { "SessionStart": [ { "hooks": [ { "type": "command", "timeout": 5,
  "command": "echo \"export PATH=\\\"$CLAUDE_PROJECT_DIR/node_modules/.bin:\\$PATH\\\"\" >> \"$CLAUDE_ENV_FILE\"; echo 'export APP_ENV=development' >> \"$CLAUDE_ENV_FILE\""
} ] } ] } }
```

`CwdChanged` and `FileChanged` get `CLAUDE_ENV_FILE` too [04 R38]. Combined with `watchPaths` output, that's a hook-driven direnv: re-export when `.envrc`-style files change.

### R7. Steer compaction with PreCompact

PreCompact runs before every compaction, with trigger `auto` or `manual` as the matcher field. It can **block** compaction (exit 2) or **add instructions** that are merged with your `/compact …` text [05 R16] [04 R33]. The spec doesn't say which output channel carries the instructions. Plain stdout on exit 0 is the plausible route (inferred), so verify on your build:

```json
{ "hooks": { "PreCompact": [ { "matcher": "auto", "hooks": [ { "type": "command", "timeout": 5,
  "command": "echo 'Preserve verbatim: the list of files modified, names of failing tests, and every decision about public API shape. Drop exploratory reads.'"
} ] } ] } }
```

Don't block auto-compaction casually. It fires because the context is nearly full. Block only for a specific, short-lived reason. Since 2.1.236 a hook can also take over compaction entirely by supplying the replacement messages [Δ 24]. The spec doesn't document that interface, so it isn't covered here.

### R8. Desktop notification when Claude needs you

```json
{ "hooks": { "Notification": [ { "hooks": [ { "type": "command", "timeout": 5,
  "command": "osascript -e 'display notification \"Claude Code needs your attention\" with title \"Claude Code\"' 2>/dev/null || notify-send 'Claude Code' 'Needs your attention' 2>/dev/null; exit 0"
} ] } ] } }
```

The matcher is compared against the notification type [04 R31]. The spec doesn't list the type values, so leave it empty and filter in the script if needed.

### Bonus: a human gate that auto mode can't skip

A hook returning `ask` forces a prompt even when the auto-mode classifier would allow the call [04 R35]. Use it for dynamic conditions a static ask rule can't express, such as "pushing while on `main`":

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command", "timeout": 5,
  "if": "Bash(git push:*)",
  "command": "[ \"$(git -C \"$CLAUDE_PROJECT_DIR\" branch --show-current)\" = main ] && echo '{\"hookSpecificOutput\":{\"permissionDecision\":\"ask\",\"permissionDecisionReason\":\"Pushing from main\"}}'; exit 0"
} ] } ] } }
```

---

## Gotchas

- **A 600 s default timeout** means a hung `command` hook can freeze the turn for 10 minutes. Always set `timeout` [04 R36].
- **Invalid regex matchers fail silently.** They never match and only log [04 R31]. Test a matcher once before relying on it.
- **Parallel hooks see the same original input.** Two hooks that both rewrite `updatedInput` don't chain. Last writer wins [04 R34].
- **Plain stdout is context only for SessionStart, UserPromptSubmit and Setup** [04 R33]. Elsewhere, use JSON `additionalContext` or exit-2 stderr.
- **Exit 1 is not a block.** Only exit 2 blocks, unless `onFailure: "block"` is set [04 R33].
- **A hook `allow` doesn't skip safety checks, deny or ask rules,** or the auto-mode classifier [04 R24, R35].
- **Hooks are unsandboxed and run with your privileges** [04 R39]. Review hooks in cloned repos and plugins as you would any script. Admins can use `allowManagedHooksOnly`.
- **Stop hooks that exit 2 can loop.** Cap them (R3) or use a prompt hook's `impossible` escape (R4) [04 R37].
- **SessionEnd gets 1.5 s by default** [04 R36]. Fire-and-forget only, or raise `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`.
- **Formatter hooks invalidate Claude's view of a file.** Expect re-reads (R2) [03 R19].
- **Plugin skills that declare PreToolUse or PermissionRequest hooks** at the top level of their frontmatter, instead of under `hooks`, are loaded with all hooks and tool grants stripped [07 R15].
