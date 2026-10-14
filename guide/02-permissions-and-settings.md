# 02 — Permissions and settings: fewer prompts without losing the brakes

> Behaviour described here is as of CLI 2.1.295 and may change. Citations point to the functional spec: `[04 R18]` = requirement, `[04#T8]` = takeaway, `[04 §4.6]` = algorithm section, `[Δ n]` = item n of `CHANGES-2.1.236-to-2.1.295.md`. "(inferred)" means the spec marks the point as inferred, or it is my reading of a gap.

Every permission prompt costs you a context switch. Every over-broad allow rule is a bet that nothing will go wrong. The goal is to let the harness auto-approve the boring 95 % and keep a human on the 5 % that matters. To do that well you need to know the order the gate runs in, because **each layer can only tighten what the previous one decided** [04 §1] [04#T2].

```
tool call → PreToolUse hooks → deny/ask/allow rules + safety checks → mode resolves any "ask" → OS sandbox (shell only)
```

> **TL;DR**
> - Rule precedence is decided by **behaviour, not source**: deny > ask > allow, whichever file each rule comes from [04 §2.3].
> - Pick the mode for the task: `plan` to explore, `acceptEdits` for routine coding, `auto` for long unattended runs, `default` for anything near production. `bypassPermissions` is for throwaway sandboxes only [04 §2.1].
> - Common read-only shell commands (`cat`, `ls`, `git log/diff/show`, `grep`, `find` without `-exec`/`-delete`, and more) are auto-allowed. Don't write rules for them [04 §4.3].
> - In auto mode, broad rules like `Bash(python:*)`, `Bash(npm run *)` or `Bash(curl …)` are **suspended**, so the classifier sees those calls [04 R11, §4.6].
> - Even in bypass mode, some hazards still need a human: deleting `/`, `~` or the workspace root, deleting a path held in a possibly-empty variable, and a few policy-driven breakers. Project settings can never turn bypass on [04 R1, R19] [Δ 28].

---

## 1. Modes, and when to use each

| Mode | What happens to an "ask" | Use it for |
|---|---|---|
| `default` | You are prompted [04 §2.1] | Unfamiliar repos, anything touching prod, credentials or infra |
| `acceptEdits` | File writes inside working dirs that pass safety checks are auto-allowed. So are shell commands whose base command is `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp` or `sed`. Everything else prompts [04 §2.1] | Day-to-day feature work where you review the diff afterwards |
| `plan` | Any non-read-only tool asks, with a note that it can't run in plan mode [04 §2.1] | Scoping a change before any write; cheap exploration |
| `auto` | Fast paths first, then an LLM classifier allows or denies [04 §4.4] | Long tasks you won't babysit, with good tests and a git safety net |
| `dontAsk` | The ask becomes a deny, with an explanation [04 §2.1] | CI and scripted runs where a prompt would hang |
| `bypassPermissions` | The ask becomes an allow, except for bypass-immune checks [04 §2.1] | Disposable containers or VMs. Nowhere you'd mind losing |

Things worth knowing:

- **Plan mode restores your previous mode** when you exit it [04 R4]. With `useAutoModeDuringPlan` (default `true`), plan mode applies auto-mode semantics to the calls it does make [04 R4].
- **`acceptEdits` includes `rm` and `mv`.** That's convenient, but it's why `acceptEdits` is "review the diff afterwards", not "walk away". Destructive-removal checks still apply (§5).
- **Auto mode is fast where it can be.** Before paying for a classifier call, it auto-allows anything `acceptEdits` would allow and any read-only call [04 §4.4]. The classifier only sees what's left [04#T4].
- **Auto mode falls back to you.** After **3 consecutive or 20 total** classifier denials, you get prompted (headless runs end the turn). After 10 calls in a row with no verdict, the turn ends with "auto mode unavailable" [04 R22, R23]. Repeated denials mean the task needs your input, not a looser rule.
- **Auto mode makes Claude try a safer route first.** When blocked, Claude is told to look for an alternative that no rule blocks (feature branch, synthetic data, narrower scope). Otherwise it must collect every blocked request and raise them together before ending the turn [04 R29]. Expect a batched list of asks, not a dead stop.

## 2. Rule syntax

Rules are `Tool` or `Tool(content)`. `Tool()` and `Tool(*)` mean the whole tool. Escape literal parentheses as `\(` `\)` [04 §2.3].

| Rule | Matches |
|---|---|
| `Bash(git status:*)` | **Prefix**: `git status` exactly, or `git status …` [04 §4.2] |
| `Bash(npm run *)` | **Wildcard**: a trailing ` *` also matches bare `npm run` [04 §4.2] |
| `Bash(make check)` | **Exact** match only |
| `Edit(src/**)` | gitignore-style glob [04 R9] |
| `Read(~/.ssh/**)` | gitignore-style glob |
| `WebFetch(domain:docs.python.org)` | A host. WebFetch rules **must** use `domain:` [04 R9] |
| `mcp__github` / `mcp__github__*` / `mcp__github__create_issue` | Whole server / all its tools / one tool [04 §2.3] |

Matching details:

- **Compound commands are split.** Each subcommand of `a && b | c` is checked separately, and the most severe result wins: deny > ask > passthrough > allow. **An allow rule must cover every subcommand** [04 R14, §4.2]. `Bash(npm test:*)` does not approve `npm test && git push`.
- **Some combinations always ask:** more than one `cd`, `cd` followed by `git` (it could load an untrusted repo's config or hooks), or creating git-internal directories and then running git [04 R14].
- **Write and Glob rules do nothing for file checks.** File checks consult only Edit and Read rules, and the validator warns about this [04 R8]. Use `Edit(...)` to cover writes.
- **Don't put a wildcard before a subcommand.** `Bash(git * status)` also matches injected options, and for git some options can execute code. The validator warns [04 R8].
- **MCP allow rules** may use a wildcard only after a literal `mcp__<server>__` prefix. Deny and ask rules may use wildcards anywhere [04 R10].
- **WebSearch rules can't contain wildcards** [04 R9].

## 3. Precedence and sources

Rules are pooled from user, project, local, flag, policy, CLI-argument, session and other sources. **Precedence is by behaviour, not by source:** a deny anywhere beats an allow anywhere [04 §2.3]. "Always allow" choices in the UI persist as rule updates with an explicit destination: user, project or local settings, session, or CLI argument [04 §2.5].

| Source | Use it for |
|---|---|
| **User** settings | Your personal defaults across all repos |
| **Project** settings (committed) | Team-wide allows for the repo's build and test commands, plus team denies |
| **Local** settings (project, uncommitted) | Your personal tweaks for this repo |
| **Policy / managed** | The organisation's ceiling. Can disable modes and lock down hooks |

(The spec names these sources but not their file paths. Check the official docs for locations.)

**Why project settings can't grant bypass:** a `defaultMode` of `bypassPermissions` set in project or local settings is **ignored**. Those files are controlled by the repository, and a cloned repo must not be able to disarm you. Only policy, user or flag sources can grant bypass [04 R1] [04#T8]. Switching to bypass at runtime also fails unless the session was launched with `--dangerously-skip-permissions`. Cloud sessions reject it outright [04 R3]. Admins can disable bypass or auto entirely with `permissions.disableBypassPermissionsMode: "disable"` and `permissions.disableAutoMode: "disable"` [04 R2].

### A starting point (project settings)

```json
{
  "permissions": {
    "allow": [
      "Bash(make check)",
      "Bash(make build)",
      "Bash(pnpm test:*)",
      "Edit(src/**)",
      "Edit(tests/**)",
      "WebFetch(domain:docs.python.org)",
      "mcp__github__get_issue"
    ],
    "ask": [
      "Bash(git push:*)",
      "Edit(migrations/**)"
    ],
    "deny": [
      "Read(~/.ssh/**)",
      "Read(**/.env)",
      "Edit(generated/**)",
      "Edit(infra/prod/**)"
    ],
    "additionalDirectories": ["../shared-lib"]
  }
}
```

And in your **user** settings, if you want routine edits auto-approved everywhere:

```json
{ "permissions": { "defaultMode": "acceptEdits" } }
```

Notes on the example: `ask` beats `allow`, so `Edit(migrations/**)` still prompts even though `Edit(src/**)` is broad. The `deny` entries win over everything, including hook approvals [04 R35].

### Tuning auto mode

You can feed the classifier your own policy with `autoMode.allow`, `autoMode.soft_deny`, `autoMode.hard_deny` and `autoMode.environment`. Each is an array of strings, and the literal `"$defaults"` splices in the built-ins [04 R26]. The built-in policy has slots for trusted repositories, sensitive remote targets and protected infrastructure scopes [04 §6]. Telling it which repo and branches are "yours" cuts false blocks.

```json
{
  "autoMode": {
    "environment": ["$defaults", "Trusted repo: github.com/acme/api; pushing feature branches there is expected"],
    "soft_deny":  ["$defaults", "Running database migrations against any non-local host"]
  }
}
```

(The spec types these as string arrays that are spliced into the classifier's policy. Free-form prose is my reading (inferred), so describe your environment plainly.) To route *every* shell call through the classifier, set `autoMode.classifyAllShell` [04 R11].

## 4. Commands that are already auto-allowed as read-only

You don't need rules for these. A real shell parser recognises them as read-only [04 R12, R13, §4.3]:

- **About 50 pure utilities**, such as `cat`, `head`, `tail`, `wc`, `stat`, `which`, `type`, `expr` and `seq`.
- **Pattern-checked commands:** `echo`, `ls`, `uniq`, `jq` (without file, env or import flags) and `find` (without `-delete`, `-exec`, `-fprint`…).
- **About 70 subcommands with flag allowlists**, including `git log/diff/show`, `gh pr view`, `docker logs`, `grep`, `sort`, `sed`, `xargs`, `tree`, `lsof` and `fd`.

A command loses read-only status if it has subshells, unsafe redirects (anything except `<`, heredocs, `>/dev/null` and fd dups), unquoted expansions, `/dev/tcp`, `git -c`/`--exec-path`/`--config-env`, or a `cd` plus `git`. Overlong commands also skip the analysis [04 §4.3]. If a "read-only" pipeline keeps prompting, one of these is usually why.

## 5. Why your broad rules vanish in auto mode

While auto mode is active, allow rules that would let Claude run arbitrary code without the classifier are **suspended** [04 R11]:

- **Interpreters and launchers:** `python`, `node`, `deno`, `tsx`, `ruby`, `perl`, `php`, `lua`, `npx`, `bunx`, `npm run`, `yarn run`, `pnpm run`, `bun run`, `bash`, `sh`, `zsh`, `fish`, `ssh`, `eval`, `exec`, `env`, `xargs`, `sudo`, and PowerShell launchers [04 §4.6].
- **Network CLIs with arguments:** `curl`, `wget`, `aws`, `gcloud`, `gsutil`.
- **Mutating `kubectl`:** `exec`, `apply`, `delete`, …
- **Any bare `Bash` or `Bash(*)` rule.**

The reason: `Bash(python:*)` can run any code at all. Once autonomy goes up, a convenience rule shouldn't quietly bypass the judge [04 §6] [04#T8]. In practice, `Bash(pnpm test:*)` survives, but `Bash(pnpm run test)` doesn't, because `pnpm run` is a launcher. Prefer rules on named scripts or `make` targets.

## 6. What still needs a human, even in bypass

Bypass turns an ask into an allow, but these still hold [04 §2.1, §4.1, R19]:

- **Deny rules and content ask rules** still apply.
- **Bypass-immune safety checks** still prompt:
  - `rm`/`rmdir` on system, home or workspace roots;
  - deleting a target that may expand to empty, such as an unset variable [04 R16];
  - since 2.1.236, any delete whose path is only known at run time: a variable set earlier in the same command, or command output [Δ 28];
  - restricted-session, read-confinement and peer-isolation checks [04 §2.4].
- **Tools that opt out of bypass**, and tools that require user interaction [04 §4.1].
- **Some checks only a person may approve**, never the classifier or a hook. Writes to Claude's own settings file are one example [04 §2.4, R19].
- **Inline `sh -c '…'` scripts are analysed recursively.** If the analysis budget runs out, it fails closed [Δ 29].

Approval prompts for dangerous actions **expire**. An unanswered prompt becomes a denial after a few minutes, "decline" is pre-selected, and there is no single-key approve [Δ 31]. If you walk away, the safe thing happens.

**Sensitive paths prompt even in `acceptEdits`.** Writes to `.git`, `.vscode`, `.idea`, `.claude`, `.husky`, `.cargo`, `.devcontainer`, shell rc files, `.npmrc`, `.mcp.json`, `.pre-commit-config.yaml` and similar raise a safety check even there. `.claude/skills`, `.claude/agents`, `.claude/commands` and `.claude/worktrees` are exempt [04 R18].

## 7. Sandbox basics

Shell commands can run in a per-command OS sandbox: Seatbelt on macOS, bubblewrap on Linux [04 §4.8]. Network traffic goes through a local proxy that enforces domain lists [04 R43]. Settings live under `sandbox.*` (the exact nesting of some keys is inferred [04 §5]):

- **`autoAllowBashIfSandboxed`** (default true): sandboxed shell calls skip whole-tool ask rules and prompts [04 R45]. This is the biggest single prompt reducer, and the sandbox is the reason it's safe.
- **`excludedCommands`** run unsandboxed but still go through permissions. They are a convenience, not a security boundary [04 R45].
- **`dangerouslyDisableSandbox`** on a call is honoured only if `allowUnsandboxedCommands` is true, and it **always** asks [04 R45].
- **`failIfUnavailable`** makes a missing sandbox a startup error. Use it in CI so you never run unconfined by accident [04 R45].
- **`network.allowedDomains`, `network.deniedDomains`, `network.strictAllowlist`:** deny is checked first, then allow. Under strict mode anything unlisted is blocked. Otherwise you're asked [04 §4.9].
- **Credential masking:** the sandbox sees sentinel values, and the proxy swaps in real secrets only for configured hosts [04 R44].
- **Always write-protected, even inside writable roots:** `.bashrc`, `.zshrc`, `.profile`, `.gitconfig`, `.mcp.json`, `.vscode`, `.idea`, `.claude/commands`, `.claude/agents`, `.git/hooks`, and `.git/config` unless `allowGitConfig` is set [04 R42].

**Hooks run outside the sandbox** [04 R39, R41]. Treat hook scripts as fully privileged code.

## 8. Reading a denial

Every decision carries a typed reason: rule, mode, hook, classifier, safety check, or per-subcommand results [04 §2.2]. When you see a block, map it to its fix:

| Reason | What to do |
|---|---|
| **rule** | A deny or ask rule matched. Find it. Remember that a deny in *any* source wins |
| **mode** | Plan or dontAsk refused it. Switch modes if the call is legitimate |
| **hook** | Your PreToolUse hook said no. Check its stderr or reason |
| **classifier** | Auto mode blocked it. Read the category and reason. Rephrase the task or name the target explicitly. A generic "go ahead" doesn't clear rules that require you to name, say, the production target [04 §6] [04#T7] |
| **safety check** | A built-in hazard. Rewrite the command; the denial message usually says how [Δ 28] |
| **subcommand results** | One part of a compound command failed. Split the command or add a rule for that part |

Claude is told that a denied action did not happen, must not be reported as done, and must not be worked around. If the action is essential, it should stop and explain [04 R30]. If Claude reports success after a denial, that's a bug worth flagging. In headless mode, asks become denies with the same guidance [04 R30].

---

## Gotchas

- **`Write(...)` rules are ignored by file checks.** Use `Edit(...)` [04 R8].
- **An allow for `npm test` doesn't cover `npm test && rm -rf dist`.** Every subcommand needs coverage [04 R14].
- **Your trusted `Bash(npm run *)` disappears the moment you switch to auto mode** [04 §4.6]. Prefer named targets.
- **A hook "allow" is not final.** Deny rules, ask rules and safety checks still apply, and in auto mode it still goes to the classifier [04 R24, R35] [Δ 34].
- **`bypassPermissions` in a committed settings file silently does nothing** [04 R1]. That's by design, not a bug.
- **`excludedCommands` is not a security control.** It only skips the sandbox [04 R45].
- **Hooks are not sandboxed** [04 R39].
- **Three classifier denials in a row hand control back to you** [04 R23]. If that keeps happening, use `autoMode.environment` to say what's normal for your project.
- **Walking away from a dangerous-action prompt is safe.** It times out to deny [Δ 31].
