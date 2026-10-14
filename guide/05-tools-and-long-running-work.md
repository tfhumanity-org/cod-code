# 05 — Tools and long-running work

> Behaviour described here is as of Claude Code CLI **2.1.295** (desktop app 2.31226.1) and may change in later builds.
> Citations point into `spec/`: `[03 R17]` = requirement R17 of `spec/03-*.md`; `[03#T3]` = takeaway T3; `[03 §4.2]` = section 4.2; `[Δ38]` = item 38 of `spec/CHANGES-2.1.236-to-2.1.295.md`. "(inferred)" marks advice that follows from the spec but is not stated in it.

> **TL;DR**
> - **Ask for independent reads and searches together.** Read-only calls run in parallel, up to 10 at once. Anything that writes runs serially, in order [03 R9–R14] [01 R10].
> - **Edit only works on a file the agent has fully read.** The harness checks content hashes, so formatters rarely break edits. When they do, the fix is "Read, then retry" [03 R17–R22].
> - **Every Bash call is a fresh shell.** The working directory carries over between calls; exported variables do not [03 R38–R39].
> - **Never sleep-poll.** Commands that time out move to the background instead of dying. Use background runs, Monitor, or desktop PR tools for waiting [03 R40–R42] [06 R46].
> - **MCP tools load lazily via ToolSearch**, so ten servers cost little prompt space. Their outputs are capped at about 25k tokens [03 R54–R56] [07 §4.5].

---

## 1. How a batch of tool calls runs

Tools start **while the model is still streaming**. A call is queued as soon as its block is complete [01 R9] [03 R10]. Concurrency is decided **per call from its parsed input** [03 R9]:

- Read, Grep, Glob, ToolSearch and similar read-only tools are concurrency-safe.
- **Bash is concurrency-safe exactly when its command is provably read-only** (`git status`, `ls`, `cat`…). Anything else is serial [03 R43] [03#T1].
- An unsafe call blocks everything queued after it. Results always come back in the original order [03 R11].
- Parallelism is capped at 10 (`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`) [01 R10].

**What to do:**
- Phrase requests so independent lookups can happen together, e.g. "read the handler, its test and the router, then propose the change". Interleaving a write between reads serialises everything after it.
- Keep read-only checks as separate, simple commands. `git status && npm test` is one serial call; `git status` alone can run in parallel with three Reads (inferred from [03 R43]).

## 2. Read, edit and staleness

### Read-before-edit
Edit and Write on an **existing** file fail unless the agent has a **non-partial** read of it on record [03 R17]. New files are exempt.
- A **partial** read means an explicit `offset`/`limit` range, or an auto-paginated read of a big file [03 §2.3, R49].
- The ledger is cleared on compaction [05 R21], so the first edit after a compaction may need a fresh Read.

### Staleness without the retry loop
If the file's mtime moved since the read:
- **Same content hash** (touched, or a formatter wrote identical bytes): the edit proceeds [03 R18].
- **Content really changed but `old_string` still matches exactly once, and the path is auto-allowed:** the edit applies and the agent is warned that the file contains changes it hasn't seen [03 R19].
- **Otherwise:** the edit fails with a message suggesting a formatter may have rewritten the file, and telling the agent to Read again [03 R19].

After a shell command that looks like a formatter or fixer (`prettier`, `black`, `ruff format`, `cargo fmt`, `--fix`, `--write`, `--in-place`…), the harness re-checks every previously-read file. It tells the agent which ones changed, naming up to 5 [03 R22]. Separately, the agent gets a diff snippet for files it read in full that changed on disk, with wording that treats the change as intentional [02 R32].

**Practical upshot:** running a formatter mid-task is fine. A stale-edit failure costs one Read. Don't disable your formatter to "help" the agent.

### Edit matching quirks [03 §4.3]
The matcher is lenient, and then restores the file's own style:
- **Curly quotes and straight quotes match each other**, and the replacement is re-curled if the file uses curly quotes.
- **Unicode escape sequences** (`é` vs `é`) match either way, and the replacement is re-escaped in the file's style.
- **Trailing whitespace is stripped** from `new_string` lines in non-Markdown files [03 R28]. If your format depends on trailing spaces outside `.md`, use a different approach.
- **Deleting a line** (empty `new_string`) also removes the trailing newline [03 R30].
- **Multiple matches fail** unless `replace_all` is set. The error states the count [03 R31], and the fix is more surrounding context.
- **Encoding, BOM and line endings are preserved** [03 R33]. `.ipynb` must go through NotebookEdit.
- **A subagent cannot write `REPORT*.md`, `SUMMARY*.md`, `FINDINGS*.md` or `ANALYSIS*.md`.** It is told to return the text instead [03 R34].

### Read limits
- Default **2,000 lines**, a 256 KiB cap and **25,000 tokens** (`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`) [03 R48].
- A whole-file read over the cap auto-paginates, with a banner giving the next offset. An explicit range over the cap is an error [03 R49].
- Images are downscaled to ≤2000×2000 and cost roughly one token per 28×28-pixel tile [03 R51].
- PDFs: at most 20 pages per call, and `pages` is required above 10 [03 R52].
- Re-reading an unchanged file with the same range returns a short "unchanged" stub [03 R21].

## 3. Big outputs spill to files

The harness spills rather than truncates [03#T7]:
- **Shell output** is cut to its head at 30k chars by default (`BASH_MAX_OUTPUT_LENGTH`, up to 150k) with an "N lines truncated" marker [03 R26].
- **Any tool result over its budget** is saved to `<project-state>/<session>/tool-results/<tool_use_id>.txt|json`. The budget is 50k chars by default and 128k for Bash. The agent gets a `<persisted-output>` block holding the size, the path and a ~2 KB preview [03 R23–R24].
- **More than 200k chars across one batch:** the largest results are spilled first [03 R25].
- **Grep** returns 250 matches by default (`head_limit`, 0 = unlimited) and truncates lines at 500 columns. **Glob** returns ≤100 paths, newest first [03 R35–R36].

**What to do:**
- Let the agent page a spilled file with Grep or ranged Reads instead of re-running the command.
- Ask for quiet modes up front: `-q`, `--reporter=dot`, `| tail`. The preview is only the first ~2 KB, so a 40k-char log whose failure is at the bottom needs an extra read (inferred from [03 R24]).

## 4. Bash: a fresh shell every call

There is **no persistent shell process**. Each command runs in a new shell that sources a snapshot taken at session start. The snapshot holds your functions, aliases, shell options and PATH [03 R38] [03 §4.5].

| Persists between calls | Does not persist |
|---|---|
| working directory (validated; reset if it leaves allowed dirs) [03 R39] | `export FOO=…`, shell variables, `source venv/bin/activate` |
| functions/aliases/PATH from the session-start snapshot | anything you add to your rc files mid-session (inferred: snapshot taken once) |
| files on disk | background jobs started with `&` inside a call (inferred) |

**What to do:**
- **Chain the setup in the same call:** `source .venv/bin/activate && pytest -q`. Better, put it in a script (chapter 07).
- **For environment that must persist**, a SessionStart (or CwdChanged/FileChanged) hook can write exports to the file named by `CLAUDE_ENV_FILE`. Those exports carry into later shell calls [04 R38].
- **Subagents are told the cwd resets between calls** and to use absolute paths [06 R14]. Brief them with absolute paths too.
- **Compound `cd` + git forces a permission prompt.** This covers more than one `cd`, `cd` followed by git, and creating git-internal dirs then running git [04 R14]. Prefer running from the repo root, or `git -C <dir>` (inferred), to avoid needless prompts.
- **Deleting a path that is only known at run time needs a human.** That covers `rm -rf "$DIR"` with a possibly-empty variable, or a path taken from a command's output [Δ28] [04 R16]. Write literal paths in scripts the agent runs.
- **Commands with control characters are rejected** [03 R44].
- The `description` field on Bash exists for *you*, the approver. It should state intent, not echo the command [03 R47].
- On native builds, `find`, `grep` and `rg` may be shadowed by bundled fast equivalents, and the Glob/Grep tools are dropped in favour of them [03 R37]. If an exotic GNU flag behaves oddly, that's why.

## 5. Timeouts, background work and waiting properly

**Timeouts.**
- Default 120 s; maximum 600 s (`BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS`) [03 R40].
- When the **main agent's** command times out it is **moved to the background, not killed**. The result gives a task id and output file [03 R40].
- A subagent's command is killed on timeout instead [03 §4.5]. Tell subagents to background long builds explicitly.

**Background runs.**
- `run_in_background` streams output to a task file. The agent is notified on completion and may Read the file for interim output [03 R41] [06 R43].
- You can background a running command with a keystroke [03 R41].
- Background shells live for 10 minutes by default [06 R43].
- As of this build, checking on background work doesn't block: a status check replaced the old blocking wait [Δ39].

**Never sleep-poll.**
- A foreground command starting with `sleep N` where N ≥ 25 is **rejected**, with a pointer to background execution or Monitor. Chaining shorter sleeps is explicitly forbidden [03 R42].
- **Monitor** turns each stdout line (or websocket frame) of a command into a notification [06 R46]:
  - timeout 5 min by default, 1 h max, re-armable;
  - lines batched every 200 ms;
  - rate-limited by a token bucket, and **killed after 30 s of suppressed output** [06 §4.6].
- Point Monitor at a *narrow* source, e.g. `tail -f log | grep --line-buffered ERROR`, not the raw firehose. No output does not mean success [03 R63].

**Slow MCP and web calls detach themselves.**
- MCP calls running past 120 s become background tasks that notify on completion [06 R44] [07 R21].
- WebFetch and WebSearch may be detached when you send a new message, so you're never stuck waiting on them [Δ38] [03 §2.2].

**All of these end in the same notification queue** [06 R43–R48]. You can keep chatting while they run. Background results go to the agent that started the work [Δ48].

## 6. Dev servers and the browser pane (desktop)

- **Declare your dev servers in `.claude/launch.json`.** The desktop app starts them with a `PORT` environment variable [00 R8] [00 §5]. Make your server honour `$PORT` and you get parallel sessions on non-colliding ports for free (inferred).
- **The preview pane** is an embedded browser with its own storage partition per tab, automated through DevTools: accessibility-tree reads, input events, script evaluation, viewport emulation [00 R8].
  - Navigating to a new origin needs per-origin approval [00 R8].
  - `$BROWSER` inside the session points at a shim that only opens **loopback URLs on ports the session owns** [00 R7]. `open http://localhost:3000` from a test script lands in the pane, but other hosts are declined.
- **Prefer real tools over pixels.** The guidance ranks dedicated MCP tools and CLIs above browser DOM tools, and browser DOM tools above clicking on screenshots [09#T13]. Each screenshot costs up to ~1,568 tokens [09 §4.1]. If your app has a CLI or an API for the thing you're checking, document it (chapter 07).

## 7. ToolSearch, deferred tools and MCP

**Deferred tools.**
- MCP tools, and several rarely-used built-ins, are **deferred**: announced by name only, with the schema loaded on demand through ToolSearch [03 R54–R55].
- ToolSearch supports exact `select:A,B` (up to 64 names), keyword search and `+required` terms [03 R56] [03 §4.7].
- **Calling a deferred tool without loading it** gives a validation error telling the agent to load the schema first [03 R4]. One wasted round trip, not a failure.

**Why many MCP servers are fine.**
- Deferred schemas don't sit in the prompt [03 §6].
- Server connection is non-blocking by default; startup doesn't wait on slow servers when tool search is available [07 R19] [Δ50].
- A server whose tool has a schema the API rejects loses only that tool [07 R18].
- What does cost you: servers marked to always load, which go in the prefix and connect blocking [07 §4.4], and **MCP instructions**, which are re-injected after compaction [05 §4.5].

**MCP limits to know.**
- Output is capped at **25,000 tokens** (`MAX_MCP_OUTPUT_TOKENS`), with a footer telling the agent to paginate or filter [07 R23] [07 §4.5]. `ENABLE_MCP_LARGE_OUTPUT_FILES` spills large outputs to files instead.
- Descriptions are capped at 2,048 chars (`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`) up front, with a larger budget once loaded [07 R18] [Δ51]. If you write an MCP server, put the key "when to use" text in the first couple of sentences (inferred).
- Idle timeout is 30 min for stdio servers and 5 min for remote ones. An interrupted call reports **"outcome unknown, verify before retrying"** [07 R21–R22]. Treat it that way yourself.

## Gotchas

- **"File has not been read" after a compaction** is normal. The ledger was cleared [05 R21].
- **A ranged Read doesn't count as a read for editing** [03 §4.2]. The agent may need a full Read of a file before editing it, which is painful for huge files (see chapter 07).
- **`export` in one Bash call is gone in the next** [03 R39]. Use one chained command, a script, or `CLAUDE_ENV_FILE`.
- **Timeouts behave differently for subagents:** the main agent's command goes to the background, a subagent's is killed [03 §4.5].
- **Monitor kills noisy sources** after 30 s of rate-limited output [06 §4.6].
- **`sleep 30 && check` is rejected outright** [03 R42], and so are chained shorter sleeps.
- **Trailing whitespace in `new_string` disappears** outside Markdown files [03 R28].
- **Shell edits aren't checkpointed** [05 R47]. `sed -i` through the harness is previewed as a diff with a base-hash check [03 R45], but rewind won't undo it.
- **Desktop PR work: never ask the agent to poll CI.** The host polls, and the agent is told not to schedule or poll CI itself [08 §3.1].
