# 04 — Permissions, Sandbox and Hooks: Functional Specification

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

Confidence tags: **observed** = behaviour seen directly in the analysed product; **inferred** = reasoned from surrounding behaviour.

---

## 1. Purpose & scope

This document specifies how an agentic coding harness decides whether a model-issued tool call may run, and how it constrains the call once allowed. Four layers apply in order, each able only to tighten the previous outcome:

```
tool_use
  -> [1] Hooks (PreToolUse)          : external programs/LLMs vote deny > ask > allow; may rewrite input
  -> [2] Rule pipeline (deterministic): deny/ask/allow rules, tool-specific checks, safety checks
  -> [3] Mode resolution of "ask"     : prompt user | auto-accept edits | deny | LLM classifier
  -> [4] OS sandbox (shell only)      : filesystem + network confinement, proxy, credential masking
```

Also in scope: the hook system in general (all events, I/O contract), the host-facing permission protocol for embedding applications (SDK/IDE/desktop).

---

## 2. Concepts & data model

### 2.1 Permission modes

| Mode | Treatment of a residual "ask" |
|---|---|
| `default` | Prompt the user (or the embedding host). |
| `acceptEdits` | Auto-allow file writes inside working directories that pass safety checks; auto-allow shell commands whose base command is `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp` or `sed`; otherwise prompt. |
| `plan` | Any non-read-only tool → ask, with a message that it cannot run in plan mode. |
| `dontAsk` | Ask → deny, with a message explaining the session doesn't prompt. |
| `bypassPermissions` | Ask → allow, except bypass-immune safety checks and tools that opt out; deny rules and content ask rules still apply. |
| `auto` | Ask → fast paths, then LLM classifier → allow or deny. Broad allow rules are suspended while active. |

Confidence: observed.

### 2.2 Decision object

```
Decision:
  behavior: "allow" | "deny" | "ask" | "passthrough"
  updated_input: dict | None
  message: str | None
  reason: DecisionReason
  suggestions: list[PermissionUpdate]     # rules the UI may offer to save

DecisionReason (tagged union on `type`):
  rule            {rule: Rule, source}
  mode            {mode}
  hook            {hook_name, reason}
  classifier      {stage, category?, reason}
  safetyCheck     {message, classifier_approvable: bool, circuit_breaker?: str}
  subcommandResults {per_subcommand: list[Decision]}
  other / asyncAgent / workingDir / ...
```

### 2.3 Rule

```
Rule:    tool_name: str ; content: str | None
Syntax:  Tool            -> whole-tool rule
         Tool(content)   -> content rule
         Tool() / Tool(*) -> whole-tool rule
         \( and \) inside content are literal parentheses
         CLI lists split on comma or whitespace outside parentheses
Sources (aggregated): userSettings, projectSettings, localSettings, flagSettings, policySettings,
                      cliArg, command, session, toolsNarrowing, mcpServerPolicy, hostCredential
Behaviors: allow | ask | deny   (precedence by behavior: deny > ask > allow, regardless of source)
```

Examples: `Bash(git status:*)` (prefix), `Bash(npm run *)` (wildcard), `Edit(src/**)`, `Read(~/.ssh/**)`, `WebFetch(domain:example.com)`, `mcp__github`, `mcp__github__*`, `mcp__github__create_issue`.

### 2.4 Circuit-breaker table

Safety checks carry an optional `circuit_breaker` key looked up in a data table:

| Breaker | bypass_immune | classifier_routed | auto_mode_deny | host_person_only |
|---|---|---|---|---|
| dangerousRemoval | yes | yes | yes | no |
| backgroundOperator | no | yes | no | no |
| suspiciousWindowsPath | no | yes | no | no |
| restrictedMode | yes | no | no | no |
| outsideReadsBlocked | yes | no | no | no |
| isolatePeerMachines | yes | no | no | no |
| claudeSettingsFile | no | no | no | yes |
| internalHardDeny | no | no | no | no |

Confidence: observed.

### 2.5 PermissionUpdate (persisted change)

```
PermissionUpdate:
  | {type: addRules|replaceRules|removeRules, rules:[{toolName, ruleContent?}], behavior, destination}
  | {type: setMode, mode, destination}
  | {type: addDirectories|removeDirectories, directories, destination}
destination: userSettings | projectSettings | localSettings | session | cliArg
```

### 2.6 Hook configuration

```
settings.hooks: { <EventName>: [ { matcher?: str, hooks: [HookSpec, ...] } ] }

HookSpec (common): type, if?: <permission-rule syntax>, timeout? (seconds), statusMessage?, once?
  command : command: str, args?: list (exec form, no shell), shell?: bash|powershell,
            async?, asyncRewake? (background; wakes model on exit 2), onFailure?: continue|block,
            timeoutFailsClosed?
  prompt  : prompt: str ($ARGUMENTS substituted), model?, continueOnBlock?
  agent   : prompt, model?
  http    : url, headers? ($VAR only for names in allowedEnvVars), allowedEnvVars?
  mcp_tool: server, tool, input (supports ${tool_input.<field>} interpolation)
```

### 2.7 Hook events (33)

PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch, Notification, UserPromptSubmit, UserPromptExpansion, SessionStart, SessionEnd, Stop, StopFailure, SubagentStart, SubagentStop, PreCompact, PostCompact, PreModelSwitch, PostModelSwitch, PermissionRequest, PermissionDenied, Setup, TeammateIdle, TaskCreated, TaskCompleted, Elicitation, ElicitationResult, ConfigChange, WorktreeCreate, WorktreeRemove, InstructionsLoaded, CwdChanged, FileChanged, DirectoryAdded, MessageDisplay. Confidence: observed.

### 2.8 Hook stdin (common)

```json
{ "session_id": "...", "transcript_path": "...", "cwd": "...",
  "prompt_id": "?", "permission_mode": "?", "agent_id": "?", "agent_type": "?",
  "effort": {"level": "?"}, "hook_event_name": "...", ...event fields }
```
PreToolUse adds `tool_name`, `tool_input`, `tool_use_id`, `mcp_server?: {name, source}`; PostToolUse adds `tool_response`, `duration_ms`. `mcp_server.source` (`sdk|plugin|user|project|...`) is the field hooks should trust, not tool-name prefixes.

### 2.9 Hook stdout JSON

```
Top level: continue?, suppressOutput?, stopReason?, decision?: "approve"|"block",
           systemMessage?, reason?, terminalSequence?, hookSpecificOutput?
  or:      {async: true, asyncTimeout?}

hookSpecificOutput by event:
  PreToolUse       {permissionDecision: allow|deny|ask|defer, permissionDecisionReason,
                    updatedInput, additionalContext}
  PreModelSwitch   same decision contract as PreToolUse
  PermissionRequest {decision: {behavior:"allow", updatedInput?, updatedPermissions?}
                             | {behavior:"deny", message?, interrupt?}}
  PostToolUse      {additionalContext, classifierContext, updatedToolOutput, updatedMCPToolOutput}
  SessionStart     {additionalContext, initialUserMessage, sessionTitle, watchPaths, reloadSkills}
  PermissionDenied {retry}
  Elicitation      {action, content}
  WorktreeCreate   {worktreePath}
  MessageDisplay   {displayContent}
  CwdChanged / FileChanged {watchPaths}
```

### 2.10 Host permission protocol

Request (harness → host, control request subtype `can_use_tool`): `tool_name, input, tool_use_id, agent_id?, mcp_server?, permission_suggestions: PermissionUpdate[], blocked_path?, decision_reason (display text), decision_reason_type, classifier_approvable, suppress_always_allow_rule, default_to_no, matched_ask_rule, requires_user_interaction, title, display_name, description, tool_kind?`.

Response: `{behavior:"allow", updatedInput?, updatedPermissions?, toolUseID?, decisionClassification?}` or `{behavior:"deny", message, interrupt?}`; `decisionClassification ∈ {user_temporary, user_permanent, user_reject}`.

---

## 3. Behavioural requirements

### Modes
- **R1** `bypassPermissions` set as `defaultMode` from project or local settings MUST be ignored (those files are repository-controlled); only policy, user or flag sources may grant it. Confidence: observed.
- **R2** Policy keys `permissions.disableBypassPermissionsMode: "disable"` and `permissions.disableAutoMode: "disable"` MUST disable those modes. Confidence: observed.
- **R3** Switching to bypass at runtime MUST fail unless the session was started with `--dangerously-skip-permissions`; distinct errors for restricted, disabled and not-launched cases. Cloud sessions MUST reject bypass. Confidence: observed.
- **R4** Entering plan mode records the previous mode and restores it on exit; `useAutoModeDuringPlan` (default true) lets plan mode apply auto semantics. Confidence: observed.

### Rule pipeline
- **R5** The pipeline MUST evaluate in the order of §4.1. Confidence: observed.
- **R6** If tool input contains unknown keys that were set aside, an allow from the tool check MUST be downgraded to passthrough. Confidence: observed.
- **R7** Bash prefix rules (`cmd:*`), wildcard rules (unescaped `*`) and exact rules MUST be distinguished (§4.2). Confidence: observed.
- **R8** The rule validator SHOULD warn when: a wildcard sits before a subcommand (it also matches injected options, which for git can execute code); `*` is mixed with trailing `:*`; rules target Write or Glob (file checks consult only Edit and Read rules). Confidence: observed.
- **R9** File-pattern tools (Read, Write, Edit, Glob, NotebookRead, NotebookEdit, Cd) match gitignore-style globs. WebFetch rules MUST use `domain:<host>`; WebSearch rules MUST NOT contain wildcards. Confidence: observed.
- **R10** MCP allow rules may use a wildcard tool name only after a literal `mcp__<server>__` prefix; deny and ask rules may use wildcards anywhere. Confidence: observed.
- **R11** While auto mode is active, allow rules that would let the model run arbitrary code without the classifier MUST be suspended (§4.6). `autoMode.classifyAllShell` suspends all shell allow rules. Confidence: observed.

### Bash analysis
- **R12** Shell commands MUST be parsed with a real shell grammar into simple commands (argv, env assignments, redirects) or flagged too-complex; overlong commands skip read-only analysis. Confidence: observed.
- **R13** Read-only fast-allow per §4.3. Confidence: observed.
- **R14** Compound commands: check each subcommand and combine by severity deny(3) > ask(2) > passthrough(1) > allow(0); whole is allowed only if every part is. Forced asks: more than one `cd`; `cd` followed by git (could load untrusted hooks/config); creating git-internal directories then running git. Confidence: observed.
- **R15** "Always allow" suggestions derive a command prefix from declarative command specs (subcommands, which args are themselves commands), recursion depth ≤2 and ≤10 steps; no model call. Confidence: observed (older versions used a model — inferred).
- **R16** `rm`/`rmdir` targeting system, home or workspace roots, or a target that may expand to empty (e.g. unset variable), MUST raise a `dangerousRemoval` safety check. Confidence: observed.
- **R17** `sed` scripts using write or execute commands, redirect-borne content, and process substitution MUST ask. A parallel analyser exists for PowerShell. Confidence: observed.

### Safety checks
- **R18** Writes to sensitive paths MUST raise a classifier-approvable safety check even in acceptEdits. Directories: `.git`, `.vscode`, `.idea`, `.claude`, `.husky`, `.cargo`, `.devcontainer`, `.yarn`, `.mvn`, `.config/git`. Files: `.gitconfig`, `.gitmodules`, `.bashrc`, `.zshrc`, `.profile`, `.envrc`, `.npmrc`, `.mcp.json`, `.claude.json`, `.pre-commit-config.yaml`, `lefthook.yml`, `gradle-wrapper.properties` and similar. Exempt: `.claude/skills`, `.claude/agents`, `.claude/commands`, `.claude/worktrees`. Confidence: observed.
- **R19** Circuit-breaker properties (§2.4) MUST be applied from the table: bypass-immune checks still prompt in bypass mode; auto-mode-deny checks are hard-denied in auto mode; host-person-only checks may only be approved by a human. Confidence: observed.

### Auto mode
- **R20** Within the ask branch, the order of §4.4 MUST hold. Mandatory-ask cases are never sent to the classifier. Confidence: observed.
- **R21** Classifier verdict parsing MUST fail closed: missing or conflicting verdict → block. Confidence: observed.
- **R22** After 10 consecutive classifier calls with no verdict, the turn MUST end with an auto-mode-unavailable message. Confidence: observed.
- **R23** Denial tracking: after 3 consecutive or 20 total classifier denials, fall back to human prompts (headless: end the turn). Any allow resets the consecutive counter. Confidence: observed.
- **R24** In auto mode a PreToolUse hook "allow" MUST still go to the classifier. Confidence: observed.
- **R25** The classifier input MUST be a curated projection (§4.5). Confidence: observed.
- **R26** Users MAY customise via `autoMode.allow`, `autoMode.soft_deny`, `autoMode.hard_deny`, `autoMode.environment` (string arrays; the literal `"$defaults"` splices in built-ins). Confidence: observed.
- **R27** Classifier model selection: remote config (per main model or single), vetted against org model policy; else a fixed mid-tier default; else the main-loop model. Temperature from `CLAUDE_CODE_AUTO_MODE_TEMPERATURE` (default 1). Confidence: observed.
- **R28** A server-side classifier MAY replace the local one, with the verdict delivered in the API response. Confidence: observed (internals unknown).
- **R29** While in auto mode, the main agent SHOULD be instructed: on a block, first try an alternative no rule blocks (feature branch, synthetic data, narrower scope); otherwise collect blocked asks and raise all of them to the user before ending the turn, never dropping one. Confidence: observed.

### Denial messaging
- **R30** Denial results SHOULD tell the model that the action did not happen, that it must not claim success, that it may use other legitimate tools but must not circumvent the denial, and that if the capability is essential it should stop and explain to the user. Headless (no prompts available) asks become denies with the same do-not-retry guidance. Confidence: observed.

### Hooks
- **R31** Matchers: empty, `*` or `.*` match all; a pattern consisting only of identifier characters and `|` (some events also allow `,`, space, `-`) is split and compared exactly (including aliases); otherwise treat as a regex over the name, its aliases and MCP family names; an invalid regex never matches and is logged. The matched field depends on the event (tool name, source, trigger, notification type, agent type, MCP server name, load reason, file basename, …). Confidence: observed.
- **R32** The `if` field filters with permission-rule syntax using the same matcher as permission rules. Confidence: observed.
- **R33** Exit codes: **0** success, stdout parsed as JSON if possible (for UserPromptSubmit, SessionStart and Setup, plain stdout becomes model context); **2** blocking — stderr fed to the model and the action blocked (Stop/SubagentStop: model is told to keep going; UserPromptSubmit: prompt erased; PreCompact: compaction blocked); **other** non-blocking — stderr shown to user only. `onFailure: "block"` converts spawn failure, timeout, other exits or invalid JSON into exit-2 semantics for blocking-capable events. Confidence: observed.
- **R34** Hooks for one event run in parallel on the same original input; command hooks are de-duplicated by command string, prompt/agent hooks by prompt+if. Decisions merge per §4.7. Confidence: observed.
- **R35** Hook/rule interplay: a hook deny ends the call; a hook allow is still subject to deny rules, ask rules and safety checks; a hook ask forces a prompt even if the classifier would allow; an allow carrying `updatedInput` satisfies tools that require user interaction. Confidence: observed.
- **R36** Timeouts: command/http/mcp_tool 600 s; prompt 30 s; agent 60 s and ≤50 turns (runs in dontAsk mode, may only Read the transcript, returns via a structured-output tool); SessionEnd 1.5 s default, up to 60 s via `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`. Timed-out output is discarded; `timeoutFailsClosed` blocks PreToolUse/UserPromptSubmit on timeout. Confidence: observed.
- **R37** Prompt hooks use structured output `{ok: bool, reason: str, impossible?: bool}` with thinking off. For Stop-type checks the evaluator receives the transcript and must cite evidence; `impossible: true` lets a goal loop terminate. If too long, retry with half the transcript. Confidence: observed.
- **R38** Only `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` are expanded in exec-form args. `CLAUDE_ENV_FILE` is provided to SessionStart, CwdChanged and FileChanged; exports written there persist into later shell calls. `CLAUDE_EFFORT` is exported. Confidence: observed.
- **R39** Settings `disableAllHooks`, `allowManagedHooksOnly`, `allowedHttpHookUrls` MUST be honoured. Hooks run outside the OS sandbox. Confidence: observed.
- **R40** PostToolUse `classifierContext` from all hooks combined (≤2000 UTF-16 units) is passed to the classifier as host-asserted intent. Confidence: observed.

### Sandbox
- **R41** Shell commands run in a per-command OS sandbox (§4.8). Hooks are not sandboxed. Confidence: observed.
- **R42** Always write-protected even inside writable roots: `.gitconfig`, `.gitmodules`, `.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`, `.ripgreprc`, `.mcp.json`, `.vscode`, `.idea`, `.claude/commands`, `.claude/agents`, `.git/hooks`, and `.git/config` unless `allowGitConfig`. Discovered by a bounded-depth file scan. Parents of protected paths MUST NOT be unlinkable/renamable. Confidence: observed.
- **R43** Network egress goes through a host-side HTTP and SOCKS proxy enforcing §4.9. Confidence: observed.
- **R44** Credential masking: the sandbox sees sentinel values; the proxy substitutes real secrets only for configured hosts and re-signs AWS SigV4 requests; per-credential mode `mask` or `deny`. Optional TLS interception with a provided CA. Confidence: observed.
- **R45** `autoAllowBashIfSandboxed` (default true) lets sandboxed shell calls skip whole-tool ask rules and prompts. `excludedCommands` run unsandboxed but still pass permissions (convenience only). `dangerouslyDisableSandbox` is honoured only when `allowUnsandboxedCommands` is true and always forces an ask. `failIfUnavailable` makes a missing sandbox a startup error. The shell tool guidance SHOULD tell the model to retry with the disable flag directly rather than asking in prose, since the retry itself is gated. Confidence: observed.
- **R46** Sandbox violations MUST be attributable to the originating command and reported back. Confidence: observed.

### Host protocol
- **R47** In headless mode the PermissionRequest hook gets first chance; then `--permission-prompt-tool` (an MCP tool) or the host's `can_use_tool` handler; if prompts are unavailable, deny. Confidence: observed.
- **R48** Embedding hosts MAY cache session-level approvals and answer repeats as `user_permanent`, and SHOULD fence project-scoped persistence before writing `updatedPermissions`. Confidence: observed.

---

## 4. Algorithms

### 4.1 Permission pipeline

```python
def decide(tool, inp, ctx):
    # step 0: hooks
    h = run_pretooluse_hooks(tool, inp, ctx)            # §4.7
    if h.behavior == "deny":
        return h
    inp = h.updated_input or inp

    d = rule_pipeline(tool, inp, ctx)
    if h.behavior == "allow":
        if d.behavior == "deny" or d.reason.type in ("rule_ask", "safetyCheck"):
            pass                                        # rules override hook allow
        elif ctx.mode == "auto":
            d = Decision("ask", reason=hook_reason)     # funnel to classifier
        else:
            return allow(inp, reason=h.reason)
    if h.behavior == "ask":
        d = d if d.behavior == "deny" else Decision("ask", hook_ask_floor=True)

    if d.behavior != "ask":
        return d
    return resolve_ask(tool, inp, d, ctx)               # §4.4 for auto, table §2.1 otherwise

def rule_pipeline(tool, inp, ctx):
    if r := match_rule(ctx.deny_rules, tool, inp, whole_or_field=True):
        return deny(reason=rule(r))
    if r := match_whole_tool(ctx.ask_rules, tool):
        if not (tool.name == "Bash" and sandbox_on() and settings.autoAllowBashIfSandboxed):
            return ask(reason=rule(r))
    d = tool.check_permissions(inp, ctx)               # parser, paths, safety checks
    if inp.has_set_aside_keys and d.behavior == "allow":
        d = passthrough(check_incomplete=True)
    for post in post_filters:
        d = post(d)
        if d.behavior == "deny":
            return d
    if r := match_content(ctx.ask_rules, tool, inp):
        d = ask(reason=rule(r)) if d.behavior != "ask" else d.with_matched_ask_rule(r)
    if tool.requires_user_interaction():
        return ask(d.reason)
    if tool.is_mcp and org_ceiling(tool) == "ask":
        return ask(reason=org_ceiling)
    if d.behavior == "ask":
        return d
    if ctx.mode == "bypassPermissions" and not tool.opts_out_of_bypass:
        if not (d.reason.type == "safetyCheck" and breaker(d).bypass_immune):
            return allow(reason=mode("bypassPermissions"))
    if d.behavior == "allow":
        return d
    if r := match_whole_tool(ctx.allow_rules, tool) or match_content(ctx.allow_rules, tool, inp):
        return allow(reason=rule(r))
    return ask(suggestions=suggest_rules(tool, inp))
```

### 4.2 Rule matching

```python
def parse_rule(s):
    m = re.fullmatch(r"([^()]+)(?:\((.*)\))?", s)      # with \( \) unescaping in content
    name, content = m.group(1), unescape_parens(m.group(2))
    if content in (None, "", "*"):
        return Rule(name, None)
    return Rule(name, content)

def bash_rule_kind(content):
    if content.endswith(":*"):          return "prefix"
    if has_unescaped_star(content):     return "wildcard"
    return "exact"

def bash_rule_matches(rule, cmd):
    kind = bash_rule_kind(rule.content)
    if kind == "prefix":
        p = rule.content[:-2]
        return cmd == p or cmd.startswith(p + " ")
    if kind == "wildcard":
        rx = ""
        body = rule.content
        trailing_any = body.endswith(" *") and body.count("*") == 1
        if trailing_any:
            body = body[:-2]
        for tok in tokenize_glob(body):
            rx += {"**/": "(?:.*/)?", "*": ".*"}.get(tok, re.escape(tok))
        if trailing_any:
            rx += "( .*)?"                 # "git *" also matches bare "git"
        return re.fullmatch(rx, cmd) is not None
    return cmd == rule.content

def file_rule_matches(rule, path, ctx):
    return gitignore_match(rule.content, path, base=rule.source_root(ctx))

def webfetch_rule_matches(rule, url):
    return rule.content.startswith("domain:") and host(url) == rule.content[7:]

def mcp_rule_matches(rule, tool_name):
    if rule.tool_name == f"mcp__{server_of(tool_name)}":       return True
    return fnmatch(tool_name, rule.tool_name)                   # validity checked at load (R10)
```

For compound commands, each subcommand is matched separately; an allow rule must cover every subcommand.

### 4.3 Bash read-only / safety check

```python
def is_read_only(cmd):
    if len(cmd) > MAX_ANALYSIS_LEN:
        return False
    ast = shell_parse(cmd)
    if ast.kind == "too-complex":
        return False
    if (ast.has_subshell or ast.has_compound or ast.has_background_amp
            or ast.bare_assignments - SAFE_ENV_NAMES
            or ast.has_unquoted_expansion or ast.has_unc_path
            or (ast.has("cd") and ast.has("git"))
            or ast.bare_repo_indicators
            or any(not r.is_safe() for r in ast.redirects)   # safe: <, <<, <&, <<<, >/dev/null, fd dup
            or ast.mentions("/dev/tcp", "/dev/udp")):
        return False
    return all(simple_is_read_only(c) for c in ast.commands)

def simple_is_read_only(c):
    name = c.argv[0]
    if name in PURE_UTILS:                      # ~50: cat, head, tail, wc, stat, which, type, expr, seq, ...
        return True
    if name in REGEX_ALLOW:                     # echo, jq (no file/env/import flags), ls,
        return REGEX_ALLOW[name].fullmatch(c.text)   # find (no -delete/-exec/-fprint...), uniq
    spec = SAFE_FLAGS.get(subcommand_key(c))    # ~70 entries: git log/diff/show..., gh pr view,
    if spec is None:                            # docker logs, grep, sort, sed, xargs, tree, lsof, fd...
        return False
    if name == "git" and any(a in ("-c", "--exec-path", "--config-env") for a in c.argv):
        return False
    return all(flag_ok(spec, a) for a in iter_flags(c.argv))   # flag types: none | number | string

SEVERITY = {"deny": 3, "ask": 2, "passthrough": 1, "allow": 0}

def check_bash(cmd, ctx):
    ast = shell_parse(cmd)
    subs = split_subcommands(ast)
    if count(subs, "cd") > 1:                    return ask("multiple directory changes")
    if cd_then_git(subs):                        return ask("cd + git may load untrusted repo config")
    if creates_git_internals_then_git(subs):     return ask("git internals created then git run")
    results = []
    for s in subs:
        if is_dangerous_removal(s):              # rm/rmdir on /, ~, workspace root, or maybe-empty $VAR
            results.append(safety("dangerousRemoval"))
        elif sed_has_write_or_exec(s) or s.has_process_substitution:
            results.append(ask())
        elif s.has_background_amp:
            results.append(safety("backgroundOperator"))
        elif simple_is_read_only(s):
            results.append(allow())
        else:
            results.append(match_rules_or_passthrough(s, ctx))
    worst = max(results, key=lambda d: SEVERITY[d.behavior])
    if worst.behavior == "allow":
        return allow(reason=subcommand_results(results))
    return worst.with_reason(subcommand_results(results))

def suggest_prefix(argv, depth=0, steps=0):
    if depth > 2 or steps > 10:
        return None
    spec = COMMAND_SPECS.get(argv[0])
    if spec and len(argv) > 1 and argv[1] in spec.subcommands:
        return [argv[0]] + (suggest_prefix(argv[1:], depth + 1, steps + 1) or [argv[1]])
    if spec and spec.arg_is_command:
        return suggest_prefix(argv[1:], depth + 1, steps + 1)
    return [argv[0]]
```

### 4.4 Ask resolution, auto mode and two-stage classifier

```python
def resolve_ask(tool, inp, d, ctx):
    m = ctx.mode
    if m == "default":       return prompt_host_or_user(tool, inp, d)
    if m == "dontAsk":       return deny(reason=mode("dontAsk"))
    if m == "plan" and not tool.is_read_only(inp):
        return ask_plan_floor()
    if m == "acceptEdits" and accept_edits_allows(tool, inp):
        return allow()
    if m == "bypassPermissions":   # only reached for bypass-immune checks
        return prompt_host_or_user(tool, inp, d)
    if m == "auto":
        return auto_mode(tool, inp, d, ctx)
    return prompt_host_or_user(tool, inp, d)

def auto_mode(tool, inp, d, ctx):
    if d.reason.type == "safetyCheck" and breaker(d).auto_mode_deny:
        return deny(fast_path="hardcoded")
    mandatory = (
        (d.reason.type == "safetyCheck" and not d.reason.classifier_approvable)
        or d.reason.type == "rule" or d.plan_floor or d.org_ceiling
        or tool.requires_user_interaction() or d.workflow_consent
        or first_read_outside_workdirs(tool, inp) or d.hook_ask_floor)
    if mandatory:
        return prompt_host_or_user(tool, inp, d)
    if accept_edits_allows(tool, inp):
        return allow(reason="would be allowed in acceptEdits")
    if tool.is_read_only(inp) or is_safe_browser_read(tool, inp):
        return allow(fast_path="allowlist")
    verdict = server_verdict() if server_classifier_enabled() else two_stage(tool, inp, ctx)
    return record_and_apply(verdict, ctx)

def two_stage(tool, inp, ctx):
    system = build_policy_prompt(user_slots=settings.autoMode)        # see §6 for content
    transcript = build_projection(ctx, tool, inp)                     # §4.5
    r1 = llm(system, transcript + STAGE1_SUFFIX, max_tokens=64,       # 256 in fast-only mode
             stop=["</block>"], deadline=stage1_deadline(tokens(transcript)),
             temperature=env_float("CLAUDE_CODE_AUTO_MODE_TEMPERATURE", 1))
    v1 = parse_verdict(r1)
    if v1 is not None and v1.block is False:
        return Verdict(block=False, stage=1)
    r2 = llm(system, transcript + STAGE2_SUFFIX, max_tokens=8192, deadline=120)
    v2 = parse_verdict(r2)
    if v2 is None:
        return Verdict(block=True, reason="parsing-error")            # fail closed
    return v2

def parse_verdict(text):
    if text is None:
        return None
    text = strip_tag(text, "thinking")
    tags = re.findall(r"<block>\s*(yes|no)\s*</block>", text)
    if len(set(tags)) != 1:
        return None
    cat = extract_tag(text, "category")
    if tags[0] == "yes" and cat not in KNOWN_RULE_SLUGS:
        cat = None
    return Verdict(block=(tags[0] == "yes"), category=cat, reason=extract_tag(text, "reason"))

def stage1_deadline(prompt_tokens):
    extra = max(0, prompt_tokens - 50_000) / 50_000 * 10
    return min(60 + extra, 120)

def record_and_apply(v, ctx):
    if v is UNAVAILABLE:
        ctx.no_verdict_streak += 1
        if ctx.no_verdict_streak >= 10:
            end_turn("auto mode unavailable")
        return prompt_or_deny()
    ctx.no_verdict_streak = 0
    if not v.block:
        ctx.consecutive_denials = 0
        return allow(reason=classifier(v))
    ctx.consecutive_denials += 1; ctx.total_denials += 1
    if ctx.consecutive_denials >= 3 or ctx.total_denials >= 20:
        return end_turn() if ctx.headless else prompt_host_or_user(...)
    return deny(reason=classifier(v))
```

Stage-1 suffix intent: bias toward blocking, ignore user intent and allow-exceptions (stage 2 handles them), judge the whole effect, begin the response with the verdict tag. Stage-2 suffix intent: follow the full classification procedure, reason first, require explicit (not implied) user confirmation to override a block.

An optional severity variant asks for a 0–100 score (50 boundary) with server-supplied thresholds.

### 4.5 Classifier input projection

```python
def build_projection(ctx, tool, inp):
    out = []
    for ev in ctx.transcript:
        if ev.is_user_text:
            out.append(("user", ev.text))
        elif ev.is_tool_use:
            t = registry[ev.name]
            if t.is_read_only(ev.input) and t.kind in ("read", "search"):
                continue                                   # reads/searches omitted
            out.append(("action", t.name, t.classifier_projection(ev.input)))
            out.append(("outcome", ev.outcome))            # ok | error | interrupted | rejected-by-user |
                                                           # blocked-by-permissions | automode-blocked |
                                                           # automode-unavailable | automode-parsing-error
        elif ev.is_assistant_text and precedes_terse_user_approval(ev):
            out.append(("referent", ev.text))              # only as context for a short "yes"
    out.append(("host_context", combined_classifier_context(limit=2000)))
    out.append(("latest_action", tool.name, tool.classifier_projection(inp)))
    return render_xml_transcript(out)
```

Projection examples: Edit → `{file_path, added lines, removed lines}`; Write → `file_path: content`; Glob → pattern only. The env var `CLAUDE_CODE_AUTO_MODE_PRIOR_ASSISTANT_CONTEXT` controls inclusion of the assistant referent.

### 4.6 Allow-rule stripping in auto mode

```python
INTERPRETERS = ["python", "python3", "python2", "node", "deno", "tsx", "ruby", "perl", "php",
                "lua", "npx", "bunx", "npm run", "yarn run", "pnpm run", "bun run",
                "bash", "sh", "ssh", "zsh", "fish", "eval", "exec", "env", "xargs", "sudo"]
NETWORK_CLIS = ["curl", "wget", "aws", "gcloud", "gsutil"]
KUBECTL_MUTATING = ["exec", "apply", "delete", ...]
PS_LAUNCHERS = ["iex", "invoke-expression", "start-process", ...]

def effective_allow_rules(rules, mode):
    if mode != "auto":
        return rules
    keep = []
    for r in rules:
        if r.tool_name in ("Bash", "PowerShell"):
            if settings.autoMode.classifyAllShell:          continue
            c = (r.content or "").rstrip(":*").strip()
            if c in ("", "*"):                               continue
            if any(c == p or c.startswith(p + " ") for p in INTERPRETERS + PS_LAUNCHERS): continue
            if c.split()[0] in NETWORK_CLIS and has_nonflag_arg(c):                       continue
            if c.startswith("kubectl") and any(v in c.split() for v in KUBECTL_MUTATING): continue
        keep.append(r)
    return keep
```

### 4.7 Hook dispatch

```python
def run_hooks(event, payload, ctx):
    if settings.disableAllHooks:
        return Empty
    specs = []
    for group in settings_hooks(event, managed_only=settings.allowManagedHooksOnly):
        if not matcher_matches(group.matcher, event_match_field(event, payload)):
            continue
        for h in group.hooks:
            if h.if_ and not permission_rule_matches(h.if_, payload):
                continue
            specs.append(h)
    specs = dedupe(specs, key=lambda h: h.command if h.type == "command" else (h.prompt, h.if_))
    results = parallel([lambda h=h: run_one(h, payload) for h in specs])   # same original input
    return merge(results)

def run_one(h, payload):
    try:
        raw = execute(h, json.dumps(payload), timeout=h.timeout or DEFAULT_TIMEOUT[h.type])
    except Timeout:
        return Block() if h.timeoutFailsClosed and event_blockable else Ignore()
    except SpawnError:
        raw = Exit(code=1)
    if raw.code == 0:
        j = try_json(raw.stdout)
        if j is None:
            if h.onFailure == "block" and raw.stdout.strip(): return Block(raw.stderr)
            return Context(raw.stdout) if event in PLAIN_STDOUT_CONTEXT_EVENTS else Ok()
        return parse_output(j)
    if raw.code == 2:
        return Block(feedback_to_model=raw.stderr)
    return Block(raw.stderr) if h.onFailure == "block" else UserNotice(raw.stderr)

def merge(results):
    decision, updated = None, None
    for r in results:
        if r.decision == "deny":
            return r                              # first deny short-circuits
        if r.decision == "ask":
            decision = "ask"
        elif r.decision == "allow" and decision is None:
            decision = "allow"
        if r.updated_input is not None:
            updated = r.updated_input             # last writer wins
    return Merged(decision, updated, contexts=[r.context for r in results if r.context])

def matcher_matches(pattern, value_set):
    if pattern in (None, "", "*", ".*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_|]+", pattern):  # some events also permit , space -
        return any(v in pattern.split("|") for v in value_set)
    try:
        rx = re.compile(pattern)
    except re.error:
        log("invalid matcher"); return False
    return any(rx.search(v) for v in value_set)
```

Async variants: a hook returning `{async: true}` or configured `async` continues in the background; `asyncRewake` wakes the model if it later exits 2.

### 4.8 Sandbox profile generation

```python
def sandbox_wrap(cmd, cfg):
    tag = "CMD64_" + b64(cmd) + "_END"
    if platform == "darwin":
        prof = ["(version 1)", f'(deny default (with message "{cfg.log_tag}"))',
                "(allow process-exec process-fork)",
                *[f'(allow mach-lookup (global-name "{s}"))' for s in MACH_SERVICES],   # ~15
                *sysctl_iokit_shm_sem_allowances()]
        if cfg.network_restricted:
            prof += [f'(allow network-outbound (remote ip "localhost:{cfg.http_port}"))',
                     f'(allow network-outbound (remote ip "localhost:{cfg.socks_port}"))']
            prof += unix_socket_and_bind_allowances(cfg)
        else:
            prof.append("(allow network*)")
        prof.append("(allow file-read*)")
        prof += [f'(deny file-read* (subpath "{p}"))' for p in cfg.deny_read]
        prof += [f'(allow file-write* (subpath "{p}"))' for p in cfg.allow_write]
        protected = cfg.deny_within_allow + scan_protected(cfg.allow_write, depth=SCAN_DEPTH)
        prof += [f'(deny file-write* (literal "{p}"))' for p in protected]
        prof += [f'(deny file-write-unlink (literal "{parent}"))' for parent in parents(protected)]
        if cfg.allowPty: prof.append(pty_allowances())
        if cfg.enableWeakerNetworkIsolation: prof.append(trust_service_allowance())
        if cfg.allowAppleEvents: prof.append(apple_event_allowance())
        return ["env", *proxy_env(cfg), "/usr/bin/sandbox-exec", "-p", "\n".join(prof),
                "bash", "-c", f"{cmd} # {tag}"]
    if platform == "linux":
        args = ["bwrap", "--new-session", "--die-with-parent", "--unshare-pid", "--unshare-user",
                "--dev", "/dev"]
        args += ["--bind", "/proc", "/proc"] if cfg.enableWeakerNestedSandbox else ["--proc", "/proc"]
        if cfg.network_restricted:
            args += ["--unshare-net"]
            args += bind_proxy_sockets(cfg)         # unix sockets bridged by socat to host proxy
        args += ro_binds(cfg.read) + rw_binds(cfg.allow_write) + ro_overlays(protected_paths(cfg))
        inner = ["apply-seccomp", "--block-af-unix"] if not cfg.allowAllUnixSockets else []
        env = {"HTTP_PROXY": "http://localhost:3128", "ALL_PROXY": "socks5://localhost:1080"}
        return args + ["--", *inner, "bash", "-c", cmd], env
    raise SandboxUnavailable   # fatal if failIfUnavailable

def watch_violations_darwin(cfg):
    for line in stream("log stream --predicate", f'eventMessage ENDSWITH "{cfg.log_tag}"'):
        cmd = decode_cmd_tag(line)            # attribute to the originating command
        report(cmd, line)
```

### 4.9 Network policy

```python
def allow_host(host, call_ctx, cfg):
    if matches_any(host, cfg.deniedDomains):     return False
    if matches_any(host, cfg.allowedDomains):    return True
    if cfg.strictAllowlist:                      return False
    if matches_any(host, call_ctx.allowed_domains):  # per-call Bash param in auto mode
        return True
    return ask_user_callback(host)
```

---

## 5. Constants & knobs

| Item | Value |
|---|---|
| Permission modes | default, acceptEdits, bypassPermissions, plan, dontAsk, auto |
| acceptEdits shell commands | mkdir, touch, rm, rmdir, mv, cp, sed |
| Severity ranks | deny 3, ask 2, passthrough 1, allow 0 |
| Prefix suggestion recursion | depth ≤2, steps ≤10 |
| Stage 1 classifier | max_tokens 64 (256 fast-only), stop at closing block tag |
| Stage 2 classifier | max_tokens 8192, deadline 120 s |
| Stage 1 deadline | 60 s + 10 s per 50k prompt tokens above 50k, cap 120 s |
| Classifier temperature | `CLAUDE_CODE_AUTO_MODE_TEMPERATURE`, default 1 |
| No-verdict limit | 10 consecutive |
| Denial limits | 3 consecutive / 20 total |
| classifierContext cap | 2000 UTF-16 units across hooks |
| Severity variant | 0–100, boundary 50, thresholds server-supplied |
| Hook timeouts | command/http/mcp_tool 600 s; prompt 30 s; agent 60 s & 50 turns; SessionEnd 1.5 s (≤60 s via `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`) |
| Linux proxy ports in sandbox | HTTP 3128, SOCKS 1080 |
| Settings keys | `permissions.{allow,ask,deny,defaultMode,additionalDirectories,disableBypassPermissionsMode,disableAutoMode}`, `useAutoModeDuringPlan`, `autoMode.{allow,soft_deny,hard_deny,environment,classifyAllShell}`, `hooks`, `disableAllHooks`, `allowManagedHooksOnly`, `allowedHttpHookUrls`, `sandbox.{autoAllowBashIfSandboxed,excludedCommands,allowUnsandboxedCommands,failIfUnavailable,allowGitConfig,allowPty,enableWeakerNetworkIsolation,enableWeakerNestedSandbox,allowAppleEvents,allowAllUnixSockets,network.{allowedDomains,deniedDomains,strictAllowlist},credentials.{files,envVars}}` |
| CLI | `--dangerously-skip-permissions`, `--permission-prompt-tool` |
| Env | `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_ENV_FILE`, `CLAUDE_EFFORT`, `CLAUDE_CODE_AUTO_MODE_PRIOR_ASSISTANT_CONTEXT`, `CLAUDE_CODE_EVAL_CONFINED` |

(Exact nesting of some sandbox keys is inferred.)

---

## 6. Rationale

- **Typed decision reasons** let UI text, telemetry, host policy and classifier eligibility all key off one discriminator rather than parsing prose.
- **Monotonic layering** means a misbehaving extension (hook, rule file) cannot widen access: an allow is always re-checked against stricter sources.
- **Data-driven hazards**: the circuit-breaker table separates "what is dangerous" from "how each mode treats it", so policy changes don't touch pipeline code.
- **Deterministic before LLM**: a real parser plus typed flag allowlists answers most calls cheaply and auditably; the classifier sees only the residue.
- **Asymmetric classifier**: a tiny block-biased first pass is cheap and safe; the expensive reasoning pass exists only to overturn blocks, so its errors are bounded to the uncertain cases. Failing closed and capping denials keeps the system usable without becoming permissive.
- **Curated projection**: by excluding raw tool output and including harness-authored outcome lines, injected content has no path to impersonate user consent.

  *Classifier policy content (described, not reproduced):* the policy frames the classifier as a security monitor for an autonomous coding agent; defines a threat model of prompt injection, scope creep and accidental damage; states that actions are allowed by default and that code quality or misreported results are out of scope; specifies a step-wise procedure (identify the latest action — treating execution of a just-written file as executing its contents; enumerate every final destination of any data being written or sent; check hard-block rules; check soft-block rules then allow-exceptions; apply explicit user intent last; generalise rules to equivalent vehicles not literally listed); and fixes an output format of a yes/no block tag plus, when blocking, a category and a one-sentence reason. Rule content is templated with environment slots (trusted repositories, sensitive remote targets, protected infrastructure scopes) and three tiers: a single **hard block** category (data exfiltration, judged by data provenance and final destination; no user context clears it); dozens of **soft block** categories covering destructive VCS operations, running code fetched from outside, production deploys and blind infrastructure applies, tampering with logs/audit/transcripts, deletions whose target cannot be verified, credential leakage, repointing remotes, agent self-modification or bypassing its own controls, poisoning instruction files, merging or approving without review, and browser-based exfiltration — each annotated with a **consent bar** stating what the user must explicitly name (e.g. the specific production target) for the block to be cleared; and mandatory **allow exceptions** (local operations, read-only operations, declared dependencies, toolchain setup, pushing to the expected git destination, writes to the memory directory and project instruction files), subject to production taking precedence.
- **Stripping broad allow rules in auto mode** prevents a pre-existing convenience rule (e.g. allow all `python`) from silently bypassing the classifier once autonomy rises; refusing repo-controlled bypass prevents a cloned repository from disarming the user.
- **Hooks with sharp exit codes** keep the extension contract simple enough for shell scripts while `onFailure`/`timeoutFailsClosed` allow fail-closed deployments; `if` filters avoid spawning processes for every shell call.
- **Sandbox + proxy + masking** contain the blast radius of an approved-but-wrong command and keep real secrets out of the sandbox entirely.

---

## 7. Harness design takeaways

- **T1** Separate the decision from its mechanism and type the reason: every verdict is a behavior plus a tagged reason (rule, mode, hook, classifier, safety check, subcommand results, …). That one field drives UI wording, telemetry, host policy and what the classifier may override; build it first.
- **T2** Layer strictly monotonically — hooks, rules, mode, sandbox — so each layer can only tighten. Re-check a hook allow against deny rules and safety checks, and still send it to the classifier in auto mode, so a buggy extension can never widen the attack surface.
- **T3** Keep the list of dangerous things in a table rather than in code: per-hazard flags (bypass-immune, classifier-routed, auto-mode-deny, human-only) let policy change without touching the pipeline, and keep things like removing a possibly-empty variable path a human decision even in bypass mode.
- **T4** Run deterministic checks first and the LLM last: a real shell parser, typed per-command flag allowlists, read-only allowlists, an "would acceptEdits allow this?" probe and a read-only tool allowlist before paying for a classifier. Cheaper, faster, and false negatives are auditable.
- **T5** Use an asymmetric two-stage classifier: a cheap, block-biased first stage (tiny token budget, stops at the verdict, ignores user intent) and an expensive reasoning stage that runs only to overturn blocks. Fail closed on parse errors and outages, and track denials so the session reverts to a human after 3 consecutive or 20 total.
- **T6** Give the judge a curated projection, not raw context: tools supply a compact classifier view of their input, reads and searches are omitted, harness-written outcome lines are added, and assistant prose appears only as the referent of a terse user approval — so injected tool output has no channel to impersonate consent.
- **T7** Attach a consent bar to each rule stating exactly what the user must name (e.g. the production target) so a generic "go ahead" cannot authorise an item slipped into a list; the bar applies at the step that actually ships the change.
- **T8** Drop broad allow rules as autonomy increases: entering auto mode suspends interpreter, launcher and wildcard shell rules that would bypass the judge; and refuse bypass mode when it comes from repository-controlled settings.
- **T9** Keep the hook contract small with sharp exit codes: 0 = JSON/context, 2 = block and show stderr to the model, anything else non-blocking, with an opt-in to fail closed. Add an `if` filter in permission-rule syntax so expensive hooks don't spawn needlessly, and offer prompt/agent hook types — an LLM verifier for stop conditions that must cite evidence and can declare a goal impossible gives cheap goal-checking loops.
- **T10** Sandbox the shell, proxy the network, mask the secrets: generate the OS profile per command, write-protect shell rc files and git hooks even inside writable roots, route all egress through a local proxy with deny-then-allow domain lists, hand the sandbox sentinel credentials that the proxy swaps per host, tag commands so violations are attributable, and tell the model to read the denial rather than guess.
- **T11** Teach the agent how to respond to denials: say the action did not happen and must not be reported as done, forbid working around it by splitting or disguising, and in auto mode direct it to try a safer alternative first and batch remaining consent requests for the user.
- **T12** Send rich, structured ask payloads to embedding hosts — default-to-no, suppress-always-allow, classifier-approvable, matched ask rule, and typed rule suggestions with a destination — so IDE and desktop hosts can render safe dialogs without parsing prose.

---

## 8. Uncertainties

- Server-side classifier: its prompt, model and latching behaviour are not visible client-side.
- The exact transcript serialiser for the classifier, its token budget and overflow/compaction thresholds were not traced.
- Full membership of the read-only tool fast-path allowlist is inferred (Read, Glob, Grep, TodoWrite and similar).
- Default severity thresholds for the severity variant are server-delivered; no client defaults found.
- The `defer` PreToolUse decision pauses a headless turn for later host resumption; where unsupported it behaves like deny (inferred). Resume protocol not traced.
- Seccomp syscall list lives in an external helper and is unknown.
- Windows sandboxing (reportedly a separate low-privilege user) was not examined.
- `CLAUDE_CODE_EVAL_CONFINED` narrows allow rules to CLI-argument sources only; purpose unclear.
- Exact settings-key nesting for some sandbox options is inferred.
