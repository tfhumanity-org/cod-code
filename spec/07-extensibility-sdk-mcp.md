# 07 — Extensibility surface & host protocol (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

This document specifies the extension points of a coding-agent CLI and the protocol through which host applications drive it:

- a single internal **command** abstraction loaded from many sources (skills, legacy command files, plugin commands, bundled skills, MCP prompts);
- skills: discovery, naming, frontmatter, budgeted listing, the `Skill` tool, inline vs fork execution;
- slash commands;
- plugins and marketplaces;
- the MCP client: configuration, naming, schema hygiene, connection policy, timeouts, output caps, resources, elicitation, OAuth;
- output styles;
- the statusline command protocol;
- the newline-delimited JSON host protocol (message stream + bidirectional control RPC) used by SDKs, IDEs, desktop and remote clients;
- remote control (relaying a local session to remote clients).

---

## 2. Concepts & data model

### 2.1 Command record

```
Command {
  kind: "prompt" | "local" | "local-ui"     # prompt = expands into a model turn;
                                           # local = returns text; local-ui = renders terminal UI
  name: str                                # namespaced, e.g. "a:b:c" or "plugin:x"
  description: str
  whenToUse?: str
  allowedTools?: [str]
  context?: "inline" | "fork"
  agent?: str
  model?: str; effort?: str|int
  hooks?: HooksConfig
  source: "policy" | "user" | "project" | "plugin" | "bundled" | "mcp" | ...
  loadedFrom: "skills" | "commands_DEPRECATED" | "syncedSkills" | "mcp" | "plugin" | ...
  path?: str                               # real path, used for dedup
}
```

Two channels expose commands:
1. **Model channel** — a budgeted listing (names + descriptions) injected as a meta reminder, plus one `Skill` tool that loads a body on demand.
2. **User channel** — slash commands, and a `commands_changed` push to hosts.

### 2.2 SKILL.md frontmatter

| key | meaning |
|---|---|
| `name` | display name; defaults to directory/file name |
| `description` | one-line summary used in listings and the Skill tool |
| `when_to_use` | appended to the description in the listing (`description - when_to_use`) |
| `allowed-tools` | tools available while the skill is active (string or list) |
| `disallowed-tools` / `disallowedTools` | tools removed while active; cleared at the next user message |
| `argument-hint`, `arguments` | slash-command argument placeholder; typed args |
| `disable-model-invocation` | model may not invoke via Skill (user-only) |
| `user-invocable` | `false` hides from users (model-only) |
| `model` | alias, full id, or `inherit` |
| `effort` | `low`/`medium`/`high`/`max`/integer |
| `shell` | `bash` / `powershell` for embedded `!` command blocks |
| `paths` | globs; skill activates only when the model touches matching files |
| `hooks` | settings-shaped hooks active while the skill is active |
| `context` | `inline` (expand into conversation) or `fork` (run as subagent) |
| `agent` | agent type for `fork` |
| `background` | fork runs in background; result via task notification |
| `fallback`, `version`, `created_by`, `improved_by` | bookkeeping |

Shared frontmatter vocabulary across skills/agents/plugins also includes: `mcpServers`, `lspServers`, `outputStyles`, `themes`, `workflows`, `channels`, `monitors`, `userConfig`, `permissionMode`, `maxTurns`, `isolation`, `memory`, `omitClaudeMd`, `hide-from-slash-command-tool`. Unknown keys are logged, not fatal.

Body substitutions: `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}`. Bundled skills that ship files get a "base directory" line prepended.

### 2.3 Skill tool contract

```
SkillInput  { skill: str, args?: str }
SkillOutput = { status: "inline", allowedTools?, model?, readOnly? }
            | { status: "forked", agentId, result, background? }
```
Max result 100,000 chars; never auto-backgrounded. An inline result also returns **context layers**: `{kind: "allowed_tools"}`, `{kind: "model"}`, `{kind: "effort"}` — temporary overrides ending at the next user message.

### 2.4 Plugin manifest (`.claude-plugin/plugin.json`)

| key | meaning |
|---|---|
| `name` | unique id, namespacing prefix (kebab-case preferred) |
| `version` (semver), `description`, `author{name,email,url}`, `homepage`, `repository`, `license` (SPDX), `keywords`, `$schema`, `metadata` | identity |
| `defaultEnabled` | default enablement; explicit `enabledPlugins` always wins |
| `hooks` | inline object or path; adds to `hooks/hooks.json` |
| `commands`, `agents`, `outputStyles`, `themes`, `workflows` | path or list; **setting one disables auto-loading of the conventional directory** |
| `mcpServers` | adds to `.mcp.json`; may reference a packaged bundle or URL |
| `lspServers` | `.lsp.json`-style: transport, `initializationOptions`, `startupTimeout`, `requestTimeout` (default 60000), `restartOnCrash`, `maxRestarts`, `diagnostics` |
| `monitors` | background watchers, deduped by name |
| `channels` | bind to an MCP server |
| `userConfig` | typed fields; `sensitive` values stored in the OS keychain; substituted as `${user_config.KEY}` into MCP env |
| `settings` | merged into user settings while enabled; allowlisted keys only |
| `binaries` | per-platform downloads pinned by sha256 into `bin/` |
| `evals` | case directory for plugin evaluation |

Marketplace: `.claude-plugin/marketplace.json`; plugin sources: `github`, `git`, `git-subdir`, `url`, `npm`, `file`, `directory`, `archive`, `command` (command prints the plugin dir; re-resolved once per session), `settings`, `seeded`, hosted-catalog, `skills-dir`. Settings: `enabledPlugins` (`name@marketplace` → bool), `extraKnownMarketplaces`, `strictKnownMarketplaces`, `blockedMarketplaces`. Pseudo-marketplace ids: `@inline` (`--plugin-dir`), `@skills-dir`, `@synced`, `@builtin`.

### 2.5 MCP server config

| `type` | fields |
|---|---|
| `stdio` (default) | `command`, `args`, `env`, `timeout`, `alwaysLoad`, `bareElicitationCapability`, `role` |
| `sse`, `http` (`streamable-http` normalised to `http`) | `url`, `headers`, `headersHelper` (script printing headers), `oauth{clientId, callbackPort, authServerMetadataUrl (https), scopes, xaa}`, `tools[{name, permission_policy}]`, `request_timeout_ms`, `discoveryCache`, `toolPermissions` |
| `sse-ide`, `ws-ide` | IDE bridge: `ideName`, `authToken` |
| `sdk` | `{type: "sdk", name}` — server lives in the host process, tunnelled over `mcp_message` |
| `claudeai-proxy` | hosted connectors: `url`, `id` |

Tool name: `mcp__<normalize(server)>__<tool>`, `normalize` replaces any char outside `[A-Za-z0-9_-]` with `_` (hosted-connector servers also collapse runs of `_`). `CLAUDE_AGENT_SDK_MCP_NO_PREFIX` drops the prefix for `sdk` servers only.

### 2.6 Host protocol frames (NDJSON over stdio)

```
# inbound user message
{ type: "user", message: {role, content}, parent_tool_use_id, uuid?, priority?: "now"|"next"|"later" }
# other inbound: keep_alive, update_environment_variables

# control (both directions)
{ type: "control_request",  request_id, request: { subtype, ... } }
{ type: "control_response", response: { subtype: "success"|"error", request_id, response?|error? } }
{ type: "control_cancel_request", request_id }

# outbound message types
system/init, assistant, user (with tool_use_result), stream_event, result,
system/<subtype>…, tool_progress, auth_status, prompt_suggestion, tombstone,
conversation_reset, command_lifecycle
```

`system/init` fields: `cwd`, `tools`, `mcp_servers[{name,status,source}]`, `model`, `permissionMode`, `slash_commands`, `terminal_slash_commands`, `output_style`, `skills`, `plugins`, `plugin_errors`, `agents`, `betas`, `apiKeySource`, `claude_code_version`.

`result` subtypes: `success`, `error_during_execution`, `error_max_turns`, `error_max_budget_usd`, `error_max_structured_output_retries`; fields `duration_ms`, `duration_api_ms`, `num_turns`, `stop_reason`, `total_cost_usd`, `usage`, `modelUsage`, `permission_denials`, `errors`, `user_message_uuid`, `structured_output`.

`system/*` subtypes include: `status`, `compact_boundary`, `api_retry`, `api_error`, `informational`, `notification`, `hook_started`, `hook_progress`, `hook_response`, `task_started`, `task_updated`, `task_progress`, `task_notification`, `background_tasks_changed`, `session_state_changed`, `permission_denied`, `elicitation_complete`, `commands_changed`, `thinking_tokens`, `files_persisted`, `local_command_output`, `model_fallback`, `turn_preempted`.

### 2.7 Permission answer

```
Allow { behavior: "allow", updatedInput?, updatedPermissions?: [PermissionUpdate], decisionClassification? }
Deny  { behavior: "deny", message, interrupt? }
PermissionUpdate =
    {type: "addRules"|"replaceRules"|"removeRules",
     rules: [{toolName, ruleContent?}], behavior: "allow"|"deny"|"ask",
     destination: "userSettings"|"projectSettings"|"localSettings"|"session"|"cliArg"}
  | {type: "setMode", mode, destination}
  | {type: "addDirectories"|"removeDirectories", directories, destination}
decisionClassification ∈ {user_temporary, user_permanent, user_reject}
```

Permission modes: `default`, `acceptEdits`, `bypassPermissions` (requires an explicit allow-dangerous opt-in), `plan`, `dontAsk` (deny unless pre-approved), `auto` (model classifier decides).

---

## 3. Behavioural requirements

### Skills
- **R1** Discovery sources, loaded in parallel: policy/managed skills dir (disable via `CLAUDE_CODE_DISABLE_POLICY_SKILLS`); user `~/.claude/skills`; synced user skills subfolder; host-pushed catalog; `--add-dir` directories; project `.claude/skills` in every directory from cwd up to the repo/home boundary; legacy `.claude/commands/**.md`; bundled skills. Duplicates MUST be removed by resolved real path. *Observed (walk boundary inferred).*
- **R2** A skill's name is its directory name; nested directories become `:`-joined namespaces relative to the base; plugin skills are prefixed `<plugin>:`. In a legacy commands directory, a `SKILL.md` wins over sibling markdown files. *Observed.*
- **R3** Files over 1 MB MUST be skipped. *Observed.*
- **R4** Dynamic discovery: when the agent touches files in a subdirectory, walk up looking for `.claude/skills` (skipping ignored and sync-owned dirs), load new skills, inject an attachment announcing them as now available, and push `commands_changed` to hosts (hosts must replace their cached list). *Observed.*
- **R5** Skills with `paths` load only when the model touches matching files. *Observed.*
- **R6** The listing MUST be budgeted (§4.1) and injected as a meta reminder introducing skills available via the Skill tool. *Observed.*
- **R7** Skill tool validation (§4.2): strip a leading `/`; resolve exact, alias, and `:suffix` matches; on ambiguity list candidates and ask for the full name; otherwise suggest within edit distance 2; if the providing plugin failed to load, say so (not "not installed"); refuse re-invocation of a skill from inside the fork that is already running it (tell the model to just follow the body); enforce `disable-model-invocation`. *Observed.*
- **R8** Inline execution MUST expand the body into new user/meta messages and return context layers (allowed tools, model, effort) that expire at the next user message. *Observed.*
- **R9** Fork execution MUST spawn a subagent (sharing file history, marked as skill-spawned), stream its progress, and return final text as `result`; `background: true` returns immediately and reports via notification. *Observed.*
- **R10** The Skill tool's guidance MUST convey: call it first when a listed skill covers the task; some skills run as subagents; background skills return only an agent name; `/name` from the user is a request to invoke; only listed or user-typed names are valid; built-in CLI commands are not skills; scoped vs unscoped names prefer the most specific scope matching the working files; if the skill's expanded content is already present this turn, follow it rather than re-invoking. *Observed (paraphrased).*
- **R11** When a user message contains `/X` naming a skill, an attachment SHOULD tell the model to invoke it only if the user is asking to run it, not if merely mentioned. *Observed.*
- **R12** In coordinator mode, the coordinator's own skill invocations are read-only: no fork, no permission grants, no hooks, no preamble shell. *Observed.*

### Slash commands
- **R13** Built-in commands are of kind local-ui, local or prompt. Custom commands are skills; `.claude/commands/*.md` is deprecated but still loaded. Hosts receive `terminal_slash_commands` (bound to the local terminal; remote/phone UIs should hide them) and `local_command_output` for command output. *Observed.*

### Plugins
- **R14** Manifest per §2.4. Load errors MUST be reported in `system/init.plugin_errors[]` with an open-set `type` (e.g. path-not-found, manifest-validation-error, dependency-unsatisfied, hook-load-failed); hosts MUST treat unknown types as generic failures. *Observed.*
- **R15** A plugin skill declaring security-sensitive hook events (PreToolUse, PermissionRequest) at the frontmatter top level instead of under `hooks` MUST load with no hooks and no allowed-tools. Plugin-granted allowed-tools MUST pass a policy gate. *Observed.*

### MCP
- **R16** Managed settings MAY deliver only `http`/`sse` servers with https URLs, no `${VAR}` expansion, and no control characters. *Observed.*
- **R17** Permission rules accept `mcp__server`, `mcp__server__*`, `mcp__server__tool`; parenthesised content patterns MUST be rejected for MCP rules. *Observed.*
- **R18** Schema hygiene on listing (§4.3): flatten top-level `oneOf`/`anyOf`; drop individual tools whose schemas the model API would reject (keep the rest of the server); cap descriptions at `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` (default 2048); strip tool-invocation-like markup from descriptions. *Observed.*
- **R19** Connection is non-blocking by default (`MCP_CONNECTION_NONBLOCKING`); `alwaysLoad` and discovery-cached servers form a blocking first group; retries back off 500/1500/4000 ms; batch sizes 3 local (`MCP_SERVER_CONNECTION_BATCH_SIZE`), 20 remote (`MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE`). *Observed.*
- **R20** A first-turn hold policy chooses whether the first turn waits for servers depending on deployment (relay, host wait/no-wait, single-shot, remote worker, coordinator, no tool search, explicit config, lifted), tunable via `CLAUDE_CODE_MCP_STARTUP_WAIT_MS`, `CLAUDE_CODE_MCP_PREWAIT_SERVERS[_MS]`, `CLAUDE_CODE_MCP_CONNECTOR_PREWAIT_MS`. A discovery cache MAY serve tool lists before a live connect (`MCP_DISCOVERY_CACHE_TTL_S`, `_MAX_STALE_S`, `_STRIKES`). *Observed.*
- **R21** Timeouts (§4.4): per-call total effectively unbounded; idle (no progress) timeout 30 min stdio / 5 min remote, none for IDE and sdk transports (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`); HTTP first-byte budget ≥60 s; auto-background after 120 s (sdk servers may opt out with `disableAutoBackground`). *Observed.*
- **R22** An interrupted call MUST return a message saying the outcome is unknown and the model should verify before assuming success or retrying. *Observed.*
- **R23** Output caps per §4.5 with a truncation footer telling the model to paginate/filter if possible, otherwise to tell the user results may be incomplete (footer overridable via `MCP_TRUNCATION_PROMPT_OVERRIDE`). `ENABLE_MCP_LARGE_OUTPUT_FILES` spills large outputs to files. Text-block `_meta` over 65,536 chars is stripped. *Observed.*
- **R24** Resources are exposed through `ListMcpResourcesTool` (each item has a `server` field) and `ReadMcpResourceTool`. MCP prompts become slash commands. MCP-served skills tell the model how to read supporting files from the server. *Observed.*
- **R25** Client capabilities: `roots{listChanged}`, `elicitation{form, url}`, optional task-scoped elicitation, the MCP Apps UI extension `io.modelcontextprotocol/ui` (mime `text/html;profile=mcp-app`). Client info names the product. *Observed.*
- **R26** Elicitation: a server's `elicitation/create` becomes an `elicitation` control request to the host; the host answers `{action: accept|decline|cancel, content}`; without a host handler the answer is `decline`; `Elicitation`/`ElicitationResult` hooks may answer programmatically; URL-mode completion emits `system/elicitation_complete`. *Observed.*
- **R27** OAuth: discover via well-known authorization-server / OpenID metadata; honour token endpoint auth method; configurable callback port (`MCP_OAUTH_CALLBACK_PORT`) and client metadata URL (`MCP_OAUTH_CLIENT_METADATA_URL`); optional cross-app access; hosts drive it via `mcp_authenticate`, `mcp_oauth_callback_url`, `mcp_clear_auth`. *Observed; internals inferred.*

### Output styles
- **R28** Locations: `~/.claude/output-styles/*.md`, project `.claude/output-styles`, plugins (`plugin:name`); files >1 MiB skipped. Frontmatter: `name`, `description`, `keep-coding-instructions` (keep default coding section alongside), `force-for-plugin`. Built-ins: default, Proactive, Concise, Explanatory, Learning; built-ins MAY carry a per-turn reminder (and Proactive a waiting-turn reminder), e.g. Concise reminds to lead with the result and drop narration but never trade correctness or safety-critical detail for brevity. Hosts may request reload. *Observed (paraphrased).*

### Statusline
- **R29** Setting `statusLine: {command, padding?, refreshInterval?}`; the command MUST NOT run unless workspace trust is accepted. Stdin JSON per §4.6. Siblings `subagentStatusLine` and `fileSuggestion` follow the same command-hook pattern. *Observed.*

### Host protocol
- **R30** SDK launch: `--output-format stream-json --input-format stream-json --verbose`; a host permission callback adds `--permission-prompt-tool=stdio` (mutually exclusive with naming an MCP tool as prompter). Other flags: `--include-partial-messages`, `--include-hook-events`, `--await-initialize`, `--setting-sources`, `--plugin-dir`, `--json-schema`, `--max-budget-usd`, `--task-budget`, `--resume`, `--resume-session-at`, `--fork-session`, `--managed-settings`, `--tools`, `--thinking-display`. *Observed.*
- **R31** `priority: "now"` MUST preempt the running turn, backgrounding (not killing) running shells, and emit `system/turn_preempted`. `update_environment_variables` MUST be refused unless the worker accepts env updates. *Observed.*
- **R32** Control requests and responses are matched by `request_id`; either side may cancel. Direction is enforced: agent-originated subtypes sent by a host MUST be rejected. Default control timeout 75 s; side questions 600 s. *Observed.*
- **R33** Host → CLI subtypes (groups): core (`interrupt`, `end_session`, `initialize`, `set_permission_mode`, `set_model`, `set_max_thinking_tokens`, `set_cwd`, `set_proactivity_level`, `claim_session`); introspection (`get_status`, `get_context_usage`, `get_session_cost`, `get_usage`, `list_models`, `get_settings`, `get_hooks_listing`, `list_permission_rules`, `get_plan`, `get_workspace_diff`, `export_conversation`); files (`read_file`, `list_directory`, `file_suggestions`, `seed_read_state`, `stage_file`, `rewind_files`, `rewind_conversation`, `fork_conversation`); MCP (`mcp_message`, `mcp_set_servers`, `mcp_status`, `mcp_reconnect`, `mcp_toggle`, `mcp_call`, `mcp_read_resource`, `mcp_authenticate`, `mcp_oauth_callback_url`, `mcp_clear_auth`, `set_mcp_permission_mode_override` — tighten-only); reloads (`reload_plugins`, `reload_skills`, `reload_output_styles`, `register_repo_root`, `add_directory`, `apply_flag_settings`, `update_settings`); host-rendered plugin UI (`ui_*`); misc (`side_question`, `submit_feedback`, `message_rated`, `remote_control`, `cancel_async_message`, `stop_task`, `channel_enable`). *Observed.*
- **R34** CLI → host subtypes: `can_use_tool` (fields `tool_name`, `input`, `permission_suggestions`, `blocked_path`, `decision_reason*`, `classifier_approvable`, `suppress_always_allow_rule`, `default_to_no`, `matched_ask_rule`, `requires_user_interaction`, `tool_kind`, `tool_use_id`, `agent_id`), `hook_callback`, `mcp_message`, `elicitation`, `request_user_dialog`, `oauth_token_refresh`, `host_auth_token_refresh`, and remote-tool legs. *Observed.*
- **R35** Hooks over the wire: `initialize.hooks = {Event: [{matcher?, hookCallbackIds, timeout?}]}`; firing sends `hook_callback{callback_id, input, tool_use_id}` and parses the hook-output schema (`continue`, `decision`, `systemMessage`, `hookSpecificOutput{permissionDecision, updatedInput, additionalContext,…}`). Pending callbacks MUST fail safe if the stream closes; a generation counter MUST retire stale callbacks on re-initialize. *Observed.*
- **R36** `initialize` request fields: `hooks`, `sdkMcpServers`, `sdkMcpServerConfigs`, `sdkMcpServerManifests`, `jsonSchema`, `systemPrompt`, `appendSystemPrompt`, `systemPromptSnapshot` (record once and reuse verbatim on later requests and resumes), `planModeInstructions`, `appendSubagentSystemPrompt`, `toolAliases` (single-hop rename), `excludeDynamicSections` (move per-user dynamic data out of the cached prompt), `agents`, `title`, `skills` (allowlist), `promptSuggestions`, `agentProgressSummaries`, `forwardSubagentText`, `supportedDialogKinds` (absence ⇒ cannot display ⇒ fail closed), `perTaskStopAffordance`, `rapidFollowupPreempt`, `forwardNestedSubagentToolCalls`, `plugins`, `workspaceTrust{accepted, directory}`. *Observed.*
- **R37** `initialize` response: `commands`, `agents`, `output_style`, `available_output_styles`, `models`, `account`, `current_model`, `current_permission_mode`, `session_state`, `hooks_applied`, `plugins_applied`, `fast_mode_state`, feedback config, remote-control flags, `capabilities`; on re-initialize also `pending_permission_requests` and `pending_user_dialog_requests` for redelivery. *Observed.*
- **R38** `session_state_changed` to `idle` is the authoritative turn-over signal, emitted after any held-back result is flushed. *Observed.*
- **R39** SDK-hosted MCP servers (§4.7). *Observed.*
- **R40** A desktop host typically configures: host permission callback and dialog handler with declared dialog kinds; per-task stop affordance; rapid follow-up preempt; file checkpointing on; setting sources user/project/local; partial messages; base hooks; managed settings; token providers; per-task disallowed tools (e.g. no user questions for scheduled tasks); and its own UI-control tools as in-process sdk MCP servers, some marked `alwaysLoad`. *Observed.*

### Remote control
- **R41** Enabled via the `remote_control{enabled, work_secret?}` control request; MUST NOT be enabled from inside a remote session. Flow: register the environment with the service (machine name, directory, branch, repo URL, max sessions, worker metadata) — a conflict response means the folder is already served on this device; long-poll for work (≈10 s timeout); attach to the session event stream with heartbeats. Remote messages queue like local prompts; permission prompts and dialogs are relayed to the remote client as `can_use_tool` / `request_user_dialog`; current model and permission mode are synced. *Observed; framing not analysed.*

---

## 4. Algorithms

### 4.1 Budgeted skill listing

```python
def skill_listing(skills, context_window=200_000):
    budget = env_int("SLASH_COMMAND_TOOL_CHAR_BUDGET") or floor(context_window * 4 * 0.01)   # ≈8000
    def line(s, with_desc=True):
        if not with_desc or s.render_mode == "name-only": return f"- {s.name}"
        desc = s.description + (f" - {s.when_to_use}" if s.when_to_use else "")
        return f"- {s.name}: {ellipsize(desc, 1536)}"
    full = [line(s) for s in skills]
    if total_len(full) <= budget: return header() + "\n".join(full)       # "fits"
    # priority mode
    pinned = [s for s in skills if s.bundled or s.always_full]
    rest   = sorted((s for s in skills if s not in pinned), key=priority_score, reverse=True)
    out, used = [], sum(len(line(s)) for s in pinned)
    out += [line(s) for s in pinned]
    for s in rest:
        l = line(s)
        if used + len(l) <= budget: out.append(l); used += len(l)
        else: out.append(line(s, with_desc=False)); used += len(s.name) + 3   # degrade, don't drop
    return header() + "\n".join(out)
```

### 4.2 Skill resolution

```python
def resolve_skill(name, registry, ctx):
    name = name.lstrip("/")
    cands = registry.exact(name) or registry.alias(name) or registry.suffix(":" + name)
    if len(cands) > 1: return Error(ambiguous, [c.name for c in cands])
    if not cands:
        if registry.failed_plugin_owns(name): return Error(plugin_failed_to_load)
        return Error(unknown, suggestions=near(name, registry.names, max_edit_distance=2))
    s = cands[0]
    if ctx.in_fork_of(s): return Error(already_running_here_follow_body)
    if s.disable_model_invocation: return Error(user_only)
    return s

def invoke_skill(s, args, ctx):
    if s.context == "fork":
        return run_subagent(agent=s.agent, prompt=render(s, args), background=s.background,
                            share_file_history=True)
    msgs = expand(render(s, args))           # $ARGUMENTS, ${CLAUDE_SKILL_DIR}, …, ! blocks via s.shell
    ctx.push_layers(allowed_tools=s.allowed_tools, model=s.model, effort=s.effort,
                    until="next_user_message")
    return Inline(new_messages=msgs)
```

### 4.3 MCP tool listing hygiene

```python
def register_tools(server, tools):
    out = []
    for t in tools:
        schema = flatten_top_level_combinators(t.inputSchema)
        if not api_accepts(schema): log_degraded(server, t.name); continue   # drop only this tool
        desc = strip_invoke_markup(t.description)[:max_desc_len()]           # 2048 default
        name = t.name if (server.type == "sdk" and env("CLAUDE_AGENT_SDK_MCP_NO_PREFIX")) \
               else f"mcp__{normalize(server.name)}__{t.name}"
        out.append(Tool(name, desc, schema))
    return out
```

### 4.4 Connection & call lifecycle

```python
async def connect_all(servers):
    blocking = [s for s in servers if s.alwaysLoad or discovery_cache.has(s)]
    await connect_batched(blocking)
    background(connect_batched([s for s in servers if s not in blocking]))   # non-blocking default

async def connect_batched(servers):
    for group, size in ((local(servers), 3), (remote(servers), 20)):
        for batch in chunks(group, size):
            await gather(*(with_backoff(connect, s, delays_ms=[500, 1500, 4000]) for s in batch))

async def call_tool(server, tool, args):
    total = min(server.timeout if server.timeout and server.timeout >= 1000
                else env_int("MCP_TOOL_TIMEOUT", 100_000_000), 2**31 - 1)
    idle = None if server.type in ("sse-ide", "ws-ide", "sdk") else \
           env_int("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", 1_800_000 if server.type == "stdio" else 300_000)
    call = start(server, tool, args, total_timeout=total, idle_timeout=idle)
    if not server.disableAutoBackground:
        call.on_elapsed(120_000, lambda: move_to_background_task(call))
    try: return cap_output(await call)
    except Interrupted: return unknown_outcome_message()
```

### 4.5 MCP output cap

```python
def cap_output(blocks, limit=None):
    limit = limit or env_int("MAX_MCP_OUTPUT_TOKENS") or 25_000
    est = sum(len(b.text) / 4 for b in blocks if b.text) + 1600 * count_images(blocks)
    if est <= 0.5 * limit: return blocks
    if text_only(blocks) and est > 4 * limit: return truncate(blocks, limit)
    if count_tokens_api(blocks) <= limit: return blocks
    return truncate(blocks, limit)

def truncate(blocks, limit):
    out, chars = [], limit * 4
    for b in blocks:
        if b.text: take = b.text[:chars]; chars -= len(take); out.append(text(take))
        elif b.image: out.append(downscale_to_fit(b, remaining_budget(chars)))
        if chars <= 0: break
    out.append(text(truncation_footer(limit)))
    return out
```

### 4.6 Statusline scheduling

```python
STDIN = dict(session_id, transcript_path, cwd, session_name,
             model={"id","display_name"},
             workspace={"current_dir","project_dir","added_dirs","git_worktree","repo"},
             version, output_style={"name"},
             cost={"total_cost_usd","total_duration_ms","total_api_duration_ms",
                   "total_lines_added","total_lines_removed"},
             context_window, exceeds_200k_tokens, fast_mode, effort={"level"}, thinking={"enabled"},
             rate_limits={"five_hour","seven_day","spend_limit"},   # each: used_percentage, resets_at
             vim={"mode"}, agent={"name"}, remote={"session_id"},
             pr={"number","url","review_state"}, worktree={...},
             prompt_cache={"warm","ttl","expires_at","hit_ratio","misses",
                           "last_miss_cause":{"causes","tools_added","system_char_delta"},
                           "recache_tokens_if_cold"})

def on_state_change(field):
    if field in WATCHED or field == "new_assistant_message": debounce(run_statusline, 300)
def schedule():
    if cfg.refreshInterval: every(max(1, cfg.refreshInterval) * 1000, run_statusline)
    t = min(next_rate_limit_reset(), cache_expiry())
    if t: at(t + 1000, run_statusline)
def run_statusline():
    if not workspace_trusted(): return
    abort_inflight(); proc = sh(cfg.command, stdin=json(STDIN)); render(proc.stdout, padding=cfg.padding)
```

### 4.7 SDK-hosted MCP with parked manifests

```python
# host side
manifests = {name: {"initialize": srv.initialize(), "tools": srv.list_tools()} for name, srv in sdk_servers}
send(control_request("initialize", sdkMcpServers=list(sdk_servers), sdkMcpServerManifests=manifests))

# CLI side
def on_initialize(req):
    for name in req.sdkMcpServers:
        m = req.sdkMcpServerManifests.get(name)
        status = ("malformed" if m and not valid(m) else
                  "protocol_version_mismatch" if m and m.version != OUR_VERSION else
                  "parked" if m else "not_honoured")
        conn = TunnelConnection(name, send=lambda msg: control_request("mcp_message",
                                server_name=name, message=msg))
        if status == "parked": conn.adopt(m)           # skip handshake round-trip
        else: conn.handshake()
```

### 4.8 Control-request dispatch

```python
pending = {}
def send_request(subtype, timeout=75, **body):
    rid = new_id(); fut = Future(); pending[rid] = fut
    write({"type": "control_request", "request_id": rid, "request": {"subtype": subtype, **body}})
    return fut.with_timeout(600 if subtype == "side_question" else timeout)

def on_line(frame):
    match frame["type"]:
        case "control_response": pending.pop(frame["response"]["request_id"]).resolve(frame)
        case "control_cancel_request": cancel_handler(frame["request_id"])
        case "control_request":
            st = frame["request"]["subtype"]
            if st in AGENT_ORIGINATED: return reply_error(frame, "direction not allowed")
            reply(frame, HANDLERS[st](frame["request"]))
        case "user": enqueue_prompt(frame, priority=frame.get("priority", "next"))
        case "keep_alive": pass
```

---

## 5. Constants & knobs

| Name | Value |
|---|---|
| Skill/rule file size cap | 1 MB |
| Output style file cap | 1 MiB |
| Listing budget | 1% of window × 4 chars/token (≈8000 at 200k); `SLASH_COMMAND_TOOL_CHAR_BUDGET` |
| Description cap in listing | 1536 chars |
| Skill suggestion edit distance | 2 |
| Skill / Agent result cap | 100,000 chars |
| MCP description cap | 2048 (`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`) |
| MCP connect backoff | 500, 1500, 4000 ms |
| MCP batch sizes | 3 local, 20 remote |
| MCP total timeout | `timeout` (≥1000) ?? `MCP_TOOL_TIMEOUT` ?? 1e8 ms, ≤ 2^31−1 |
| MCP idle timeout | 30 min stdio; 5 min remote; none IDE/sdk |
| HTTP first byte | ≥60 s |
| MCP auto-background | 120 s |
| MCP output cap | 25,000 tokens (`MAX_MCP_OUTPUT_TOKENS`); 4 chars/token; image 1600; bands 0.5× / 4× |
| `_meta` strip threshold | 65,536 chars |
| LSP request timeout | 60,000 ms default |
| Statusline | 300 ms debounce; refresh ≥1 s; +1 s after reset/expiry |
| Control timeouts | 75 s; side question 600 s |
| Env | `CLAUDE_CODE_DISABLE_POLICY_SKILLS`, `CLAUDE_AGENT_SDK_MCP_NO_PREFIX`, `MCP_CONNECTION_NONBLOCKING`, `MCP_SERVER_CONNECTION_BATCH_SIZE`, `MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE`, `CLAUDE_CODE_MCP_STARTUP_WAIT_MS`, `CLAUDE_CODE_MCP_PREWAIT_SERVERS[_MS]`, `CLAUDE_CODE_MCP_CONNECTOR_PREWAIT_MS`, `MCP_DISCOVERY_CACHE_TTL_S` / `_MAX_STALE_S` / `_STRIKES`, `MCP_TOOL_TIMEOUT`, `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`, `MAX_MCP_OUTPUT_TOKENS`, `MCP_TRUNCATION_PROMPT_OVERRIDE`, `ENABLE_MCP_LARGE_OUTPUT_FILES`, `MCP_OAUTH_CALLBACK_PORT`, `MCP_OAUTH_CLIENT_METADATA_URL` |
| Settings | `enabledPlugins`, `extraKnownMarketplaces`, `strictKnownMarketplaces`, `blockedMarketplaces`, `statusLine`, `subagentStatusLine`, `fileSuggestion`, `outputStyle` |

---

## 6. Rationale

- **One command abstraction** means listing, permissions, telemetry and host pushes work uniformly regardless of source; real-path dedup avoids double-loading the same file through two roots.
- **Progressive disclosure** keeps the per-request prompt small: only names and short descriptions are always present; bodies cost tokens only when used; degrading to name-only preserves discoverability under pressure.
- **Context layers** let a single skill file express both a procedure (inline, with temporary tool/model/effort changes) and a delegation (fork).
- **MCP output is untrusted volume**: tiered estimation avoids paying for exact token counts in the common cases while still bounding context use; dropping only bad tools keeps a server usable.
- **Non-blocking MCP startup** makes first-turn latency independent of slow servers; the discovery cache and tool-search deferral keep tool lists available and requests small.
- **Typed control RPC with direction rules and open enums** lets many hosts (terminal, desktop, IDE, web, phone, SDK) share one engine and evolve independently; failing closed on undeclared capabilities avoids prompts nobody can answer.
- **In-host tool tunnelling** avoids spawning processes for host-provided tools; pre-sent manifests remove a handshake round-trip.
- **Statusline gated on trust** because it runs arbitrary shell; scheduled refreshes at known change points keep it accurate cheaply.

---

## 7. Harness design takeaways

1. **T1 One command abstraction, many sources.** Load skills, legacy commands, plugin commands, MCP prompts and bundled skills into one record with source tags; dedupe by real path; namespace by directory and plugin.
2. **T2 Progressive disclosure under a hard character budget.** About 1% of the window for the listing, a per-description cap, degrade lowest-priority entries to name-only, load bodies on demand, and concatenate description with when-to-use.
3. **T3 Skills return context layers, not just text.** Activation injects messages and temporary tool/model/effort overrides ending at the next user message; a fork setting turns the same file into a subagent.
4. **T4 Guard against loops and misfires.** Block fork recursion, suggest near names and list ambiguous ones, tell the model not to re-invoke loaded instructions, and distinguish mentioning `/name` from invoking it.
5. **T5 Treat MCP output as hostile to the context window.** Default token cap with a three-band estimator, proportional media truncation, a paginate-instead footer, description caps, per-tool schema rejection, combinator flattening.
6. **T6 Never block startup on MCP.** Non-blocking connects with a small always-load group, separate local/remote concurrency, a discovery cache, backoff retries, and tool-search deferral.
7. **T7 Distinguish "no progress" from "slow".** Long total timeout plus idle timeout, auto-background past a threshold, and an "outcome unknown, verify first" message on interruption.
8. **T8 Make the host protocol a typed RPC beside the event stream.** Request ids, cancellation, direction rules, additive fields, open-set enums with a generic fallback, and default timeouts.
9. **T9 Fail closed on undeclared host capabilities.** Missing dialog kinds mean "cannot display"; permission answers can carry edited input and persistent rule updates with explicit destinations; host hook callbacks are generation-tracked.
10. **T10 Tunnel in-host tools instead of spawning servers.** In-process MCP servers reached via a message tunnel, with pre-sent handshake manifests to skip a round-trip.
11. **T11 Give the statusline a rich, stable JSON contract.** Model, workspace, cost, context, rate limits with reset times, cache warmth and miss causes; debounce, schedule refreshes at known change points, gate on trust.
12. **T12 Make plugin manifests explicit and fenced.** Conventional directories by default but explicit paths disable auto-loading; sha-pinned binaries; allowlisted settings merges; misplaced security hooks load as no hooks and no grants; managed MCP limited to https without env expansion.

---

## 8. Uncertainties

- The priority score used to order skills under budget pressure was not traced (probably usage recency/frequency).
- Whether project skill discovery stops at the repo root or the home directory is unconfirmed.
- Execution details of embedded `!` shell blocks and positional argument substitution were not read.
- Tool-search deferral thresholds and delta attachments belong to another area and were only partly seen.
- OAuth internals (dynamic registration vs metadata document, token storage, refresh scheduling) are inferred from configuration names only.
- The full `result` field list and SDK-side partial-message assembly were skimmed.
- Remote-control transport framing, heartbeat intervals and secret handling were not analysed.
- The plugin-drawn host UI pane protocol exists (~25 subtypes) but its component model was not examined.
