# 07 — Agent-friendly repositories

> Behaviour described here is as of Claude Code CLI **2.1.295** (desktop app 2.31226.1) and may change in later builds.
> Citations point into `spec/`: `[02 R19]` = requirement R19 of `spec/02-*.md`; `[03#T11]` = takeaway T11; `[08 §4.5]` = section 4.5; `[Δ35]` = item 35 of `spec/CHANGES-2.1.236-to-2.1.295.md`; `P16` = top pattern 16 in `spec/INDEX.md`. "(inferred)" marks advice derived from harness behaviour rather than stated in the spec.

This chapter is about the repository rather than the prompt. The harness has hard limits: a 2-minute default shell timeout, 25k-token reads, a fresh shell per command, spill files, and a CI poller that dedupes by check name. A repo built with those limits in mind gets better results from every session, with no prompt engineering.

> **TL;DR**
> - **Document one fast, deterministic, quiet test command in CLAUDE.md** (or AGENTS.md). Keep it under 2 minutes, or tell the agent to background it [03 R40] [02 R21a].
> - **Prefer small files.** A Read stops at 2,000 lines / 25k tokens, and Edit needs a full read [03 R17, R48–R49].
> - **Put knowledge in scripts, not in shell state or tribal lore.** Each Bash call is a fresh shell, and one script needs one allow rule [03 R38–R39] [04 R7].
> - **Fence secrets with sandbox read-denies, the read-confinement setting and credential masking**, not with "please don't read .env" [04 R41–R44] [Δ35].
> - **Give CI stable, distinct check names and deterministic tests**, so the desktop auto-fix loop has something it can act on [08 R10.2, R10.4].

---

## 1. Instructions the harness actually loads

- **Where they live.** CLAUDE.md, `.claude/CLAUDE.md`, `.claude/rules/**/*.md` and CLAUDE.local.md load from the filesystem root down to the cwd [02 R15].
  - Projects with no CLAUDE.md get their **AGENTS.md** loaded by default [02 R21a] [Δ26]. One file can serve several agent tools.
  - Imports with `@path` work up to 5 levels deep [02 R17].
- **Scale for monorepos.**
  - A rules file with `paths:` frontmatter loads only when the agent touches a matching path [02 R19].
  - A CLAUDE.md in a subdirectory arrives when a file under it is read or written [02 R20].
  - Put package-specific rules next to the package, not in a 900-line root file.
- **Size.** A file over max(40k chars, 5% of the window) triggers a warning, and the total has its own threshold. Nothing is truncated, but every byte is in every request [02 R18]. Keep the root file to what's needed on *every* task.
- **Editing is cheap now.** Instruction files are sent as change-only messages, so updating CLAUDE.md doesn't invalidate the session's cache [Δ16]. It is also re-stated after compaction [Δ18]. When the agent learns a lesson the hard way, write it down.

**What belongs in the root CLAUDE.md:**
- The exact **test, lint, typecheck and build commands**, with expected runtimes.
- How to run **one** test (file or name filter).
- **Where things are**: entry points, where generated code lives, what never to edit by hand.
- **Durable constraints** written as imperatives ("Never edit `db/migrations/` by hand; run `make migration`").

## 2. A fast, deterministic test command

- **Under 2 minutes, or say so.** The shell timeout defaults to 120 s and goes up to 600 s [03 R40].
  - Past the timeout, the main agent's command moves to the background. A subagent's is killed [03 §4.5].
  - If the full suite takes 8 minutes, document a fast subset and tell the agent to run the full suite in the background [03 R41].
- **Quiet by default.** Shell output is cut at 30k chars, and anything over budget spills to a file with only a ~2 KB preview [03 R24, R26].
  - A test runner that prints 5,000 passing dots buries the one failure.
  - Offer a `-q` mode that prints failures and a one-line summary (inferred from [03 R24]).
- **Deterministic.** Flaky tests are the worst case for an agent. It will "fix" code that isn't broken.
  - The desktop CI poller dedupes failures per **head commit and check name**, so the same flake on the same head is reported only once [08 R10.2]. A flake that passes on rerun still sent the agent chasing it the first time (inferred).
- **Single-test targeting.** Make "run just this test" a documented one-liner. A tight edit→test loop is cheap and lets the agent avoid long runs.
- **Exit codes that mean something.** The agent reads exit status and output. "Passed with 3 errors" is noise.

## 3. Formatters and linters: hooks vs the agent

The harness is built to tolerate formatters:
- An edit to a file whose mtime moved but whose content hash is unchanged just proceeds [03 R18].
- If the content changed but the edit still matches once, it applies with a warning [03 R19].
- After a formatter-like shell command, the agent is told which previously-read files changed [03 R22]. The agent also receives diffs of files it had read that changed on disk [02 R32].

**Options, in order of preference:**
1. **Have the agent run the formatter as part of the documented workflow** ("after editing, run `make fmt`"). The harness recognises formatter commands (`--write`, `--fix`, `prettier`, `black`, `ruff format`, `cargo fmt`, `go fmt`…) and refreshes its view [03 §4.2]. This is cheap and visible.
2. **A PostToolUse hook on Edit/Write that formats.** It is deterministic and the agent can't forget it.
   - Each formatted file then differs from what the agent last saw, so expect stale-edit warnings or an extra Read on files that are edited repeatedly (inferred from [03 R19]).
   - Hooks run **outside** the sandbox [04 R39], with a 600 s timeout [04 R36].
3. **Lint feedback via a hook that exits with code 2.** The hook's stderr is fed to the model as a blocking message [04 R33]. Keep that stderr short and actionable: file, line, rule, fix.
4. **A Stop hook as the final gate.** "Don't finish while lint fails." After 8 consecutive blocks the turn ends anyway, with a warning to hook authors [01 R6]. A gate that can never pass just burns turns.

**Opinion:** format in the workflow or on Stop, and lint on Stop or in pre-commit. Per-edit hooks are fine for fast formatters but add friction for slow linters.

## 4. File size and layout

- **Read limits:** 2,000 lines by default, a 256 KiB cap, and **25k tokens** [03 R48]. Bigger files auto-paginate with a "next offset" banner, and the agent is warned not to answer from the first page alone [03 R49].
- **Edit wants a full read.** Edit and Write fail on files whose only read was partial [03 R17] [03 §4.2]. A 6,000-line module makes the agent page through it or fall back to shell edits, which **rewind can't undo** [05 R47]. Small, cohesive files are the single best structural change you can make (inferred).
- **Long lines.** Grep truncates matched lines at 500 columns [03 R35]. Keep minified bundles, lockfiles and generated blobs out of search paths. Grep already skips VCS dirs, and on native builds the bundled grep honours ignore files [03 R37].
- **Glob returns ≤100 paths, newest first** [03 R36]. Predictable directory names (`src/<feature>/`, `tests/<feature>/`) make narrow globs possible.
- **Notebooks** are edited through NotebookEdit, cell by cell [03 R33]. Logic that lives in `.py` modules is easier for the agent than logic buried in cells (inferred).
- **Git status in context is capped at 2,000 chars** [02 R13]. A tree full of untracked build artifacts crowds out the useful part. Gitignore them.

## 5. Clear error messages pay twice

The harness itself is designed around errors that name the fix: "did you mean…", "Read the file again", "use 0 for unlimited" [03#T11] [P16]. Your code gets the same benefit:
- **Say what to do, not just what broke.** "Config key `db.url` missing; copy `config.example.toml`" saves a search.
- **Include the identifier.** Name the file, the key, the test or the env var. Agents grep for whatever you print.
- **Fail fast at startup** on missing config, rather than with a stack trace three layers deep.
- **Distinguish "not found" from "not allowed"** in CLIs and scripts. Denial messages that explain the situation stop retry loops [04 R30] [Δ28].

## 6. Scripts over tribal knowledge

Every Bash call is a **fresh shell**. The cwd carries over; exported variables and activated virtualenvs do not [03 R38–R39]. So:
- **Make entry points self-contained.** `./scripts/test` should find its interpreter, load env and run. It should not depend on "first `source .envrc`".
- **For a persistent session environment**, a SessionStart hook can write exports to `CLAUDE_ENV_FILE`. They then apply to later shell calls [04 R38].
- **Fewer permission prompts.** A prefix rule (`cmd:*`) allows a whole script with one rule [04 R7]. `./scripts/check:*` is easy to approve once. A dozen ad-hoc pipelines are not.
- **Avoid patterns that force prompts.** These include compound `cd … && git …`, more than one `cd` in a command, and `rm` of a path held in a variable or taken from command output [04 R14, R16] [Δ28]. Write scripts with literal paths and run git from the repo root.
- **Don't rely on interactive tools.** Pagers, prompts and TUIs hang or time out (inferred). Provide `--yes` / `--no-pager` / `CI=1` paths and document them.
- **Prefer CLIs and APIs over GUIs.** The harness steers toward dedicated tools and CLIs over browser automation, and toward browser automation over screenshot clicking [09#T13]. A `make seed-db` beats "click through the admin panel".

## 7. `.claude/launch.json` for apps (desktop)

- List your dev servers in `.claude/launch.json`. The desktop app starts them with a **`PORT`** environment variable [00 R8] [00 §5].
  - **Honour `$PORT`.** Parallel sessions in separate worktrees then don't fight over 3000 (inferred).
- The preview pane only accepts **loopback** URLs on ports the session owns, through the `$BROWSER` shim [00 R7]. Serve the app on localhost and print the URL so it can be opened there.
- Per-origin approval applies to navigation [00 R8]. Keep the dev app on one origin where you can: no cross-domain auth redirects in development.
- Add local config the app needs (e.g. `.env.local`) to **`.worktreeinclude`**, so app-made worktrees get it [08 R8.3].

## 8. Keep secrets out of reach

Instructions are not a security boundary; fences are.
- **The OS sandbox** wraps shell commands per call [04 R41]. It can deny reads of specific paths and limit writes to allowed roots [04 §4.8].
  - Rc files, `.git/hooks`, `.git/config`, `.mcp.json`, `.claude/agents` and `.claude/commands` are write-protected even inside writable roots [04 R42].
- **Read confinement.** As of this build a read-confinement setting covers file tools in every mode, shell commands with run-time paths (which go to a person) and the sandbox [Δ35]. Without it, a write-focused setup still leaves secrets readable.
- **Network egress** goes through a proxy with denied and allowed domain lists, plus an optional strict allowlist (`sandbox.network.*`) [04 R43] [04 §4.9].
- **Credential masking.** The sandbox sees sentinel values, and the proxy substitutes real secrets only for configured hosts (`sandbox.credentials.*`) [04 R44]. This is the right pattern for API keys that tests need.
- **Read deny rules** are also applied to Grep, as ignore globs, and to its results [03 R35]. Instruction files that permissions deny are withheld rather than loaded [02 R20].
- **Repository-controlled settings can't grant bypass mode** [04 R1]. A cloned repo can't silently turn off your prompts.
- **Writes to sensitive paths always prompt**, even in accept-edits mode. These include `.git`, `.claude`, `.husky`, `.envrc`, `.npmrc`, `.pre-commit-config.yaml` and similar [04 R18].

**Practical setup:**
- Keep real secrets outside the repo, or in a path the sandbox read-denies.
- Commit a `.env.example`.
- Give tests fake credentials or masked ones.

## 9. CI that the auto-fix loop can act on (desktop)

The desktop poller reads checks, reviews and comments for your PR, then wakes the agent with its findings [08 §4.5]:
- **Stable, distinct check names.** Failures are keyed by `head:checkName`, with names truncated to 120 chars [08 §4.5].
  - Two jobs called "test" collide.
  - Names that embed timestamps or run ids defeat dedup (inferred).
- **Group checks into workflows sensibly.** A failure is held up to 5 minutes while sibling checks in the same workflow are pending [08 R10.4]. One giant workflow delays feedback; dozens of tiny ones fragment it (inferred).
- **Readable failure logs.** The agent fixes what it can read. Put the failing test name and assertion near the end of the job output, and avoid 50 MB logs (inferred).
- **Who can steer it.** Comments are forwarded only from bots and owner/member/collaborator accounts, never the session's own login [08 R10.3]. Quoted comment text is data, not authorisation [08 R10.6]. Make linters and bots post actionable comments.
- **Merge-friendly history.** The agent merges the base in and never rebases or force-pushes [08 §3.1]. For conflicts, the app attempts a host-side merge first [08 R10.5]. If your policy requires linear history, squash at merge time rather than expecting the agent to rebase.

## 10. Commit hygiene for agent branches

- **Branch naming.** App-made branches use a configurable prefix, `claude` by default [00 §5]. Protect `main` and let agents work on prefixed branches. The auto-mode judge sees branch vs default branch and clean vs dirty state [Δ33], and is steered to prefer safer alternatives such as feature branches when it blocks something [04 R29].
- **Commit often.** Rewind only restores edits made through the edit tools [05 R47]. A commit is your real checkpoint for codegen, installs and migrations.
- **Never use bare `git stash` in multi-worktree setups.** The stash stack is shared across worktrees [02 R10]. Child sessions are told not to stash, reset or switch branches in a shared checkout, and to commit only their own files [08 §3.1].
- **Meaningful commit subjects.** The last 5 one-line commits are in every session's starting context [02 R13]. "wip" ×5 tells the agent nothing.
- **Clean up kept worktrees.** Agent worktrees with changes are kept, not deleted [06 R56].
- **Expect runtime-file excludes.** The CLI adds a marked block to `.git/info/exclude` for its own runtime files (scheduled tasks, worktrees…) [06 R50]. You don't need to add them to `.gitignore`.

## Gotchas

- **AGENTS.md is used only where a directory has no CLAUDE.md** in the default mode. Both can be loaded via a setting [02 R21a].
- **A huge CLAUDE.md is never truncated** [02 R18]. You pay for all of it on every request.
- **A test suite longer than 10 minutes cannot run in the foreground** [03 R40]. Document the background path.
- **Hooks run unsandboxed** [04 R39]. Review hook scripts like production code.
- **A Stop-hook gate that can't pass** ends the turn after 8 blocks [01 R6].
- **Formatting hooks cause stale-edit warnings** [03 R19]. They are harmless but noisy.
- **Duplicate CI check names** collide in the dedup key [08 §4.5].
- **Shell-generated changes aren't rewindable** [05 R47]. Commit before codegen.
