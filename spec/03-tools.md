# 03 — Built-in Tool Suite: Functional Specification

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

Confidence tags: **observed** = behaviour seen directly in the analysed product; **inferred** = reasoned from surrounding behaviour.

---

## 1. Purpose & scope

This document specifies the tool layer of an agentic coding harness: how tools are declared, how a batch of model-issued tool calls is validated, scheduled (parallel vs serial), executed and turned into tool results, and the specific behaviour of the core file, search, shell and meta tools.

In scope: tool declaration contract, per-call pipeline, concurrency scheduler, read ledger and staleness rules, edit string matching, output size budgets and spill-to-disk, shell execution model, Read media handling, deferred tools and the search tool, notable behaviours of auxiliary tools, model-dependent prompt variants.

Out of scope (see doc 04): permission rules, modes, classifier, hooks, OS sandbox. Context compaction and subagent internals are covered elsewhere.

---

## 2. Concepts & data model

### 2.1 Tool declaration

Every tool is a declarative record merged over a set of defaults. Fields an implementation needs:

```
ToolDef:
  name: str                          # public name sent to the model
  aliases: list[str] = []            # legacy names still accepted
  input_schema: Schema               # validated after coercion
  output_schema: Schema | None
  description(ctx) -> str            # short description
  prompt(model, lean: bool, tools) -> str   # long usage guidance; two variants
  search_hint: str = ""              # few words used by the tool-search scorer
  max_result_chars: int | INF        # per-tool result budget (see §5)
  persistence_ceiling: int | None    # overrides the global spill ceiling
  skip_batch_budget: bool = False    # exempt from per-message aggregate budget
  backgrounding: "never" | "self" | "detach" = "never"
  should_defer: bool = False         # announced by name only; schema loaded on demand
  always_load: bool = False          # cancels deferral
  is_enabled() -> bool = True
  is_read_only(input) -> bool = False
  is_concurrency_safe(input) -> bool = False
  is_destructive(input) -> bool = False
  requires_user_interaction() -> bool = False
  rule_content_field: "file_path" | "command" | "path" | None   # used for permission-rule matching
  get_path(input) -> str | None
  coerce_input(raw) -> (input, notes[])   # repair near-miss shapes
  validation_error_steer: str | None      # extra hint appended to schema errors
  classifier_projection(input) -> str = ""  # compact text for the safety classifier (doc 04)
  create(call_ctx) -> { validate_input, check_permissions, call }
  map_result(data, tool_use_id) -> ToolResultBlock
```

Default posture: enabled, **not** read-only, **not** concurrency-safe, not destructive. A tool must opt in to parallelism. Confidence: observed.

`create()` is invoked per call so each call gets a fresh context view (abort signal, tool-use id, file reading limits, sibling tool uses in the same turn, progress callback, turn-abort). If `check_permissions` is absent the tool is treated as permission-passthrough-allow (the global pipeline still applies). Confidence: observed.

If the "execution location" of a call (e.g. which attached machine it targets) changes between the permission check and execution, the call must be refused and the model told to resend. Confidence: observed.

**Multi-machine routing (optional):** when remote machines are attached, file and shell tools gain an optional `_host: str` input naming the target machine. Routable tools: Bash, PowerShell, Read, Write, Edit, NotebookEdit, Glob, Grep, SendUserFile, Monitor. Confidence: observed (only skimmed).

### 2.2 Tool inventory

RO = read-only, CS = concurrency-safe. "Deferred" = schema loaded via ToolSearch.

| Tool (aliases) | Input fields | RO | CS | Result size limit | Deferred |
|---|---|---|---|---|---|
| Bash | `command: str`, `timeout?: int ms`, `description?: str`, `run_in_background?: bool`, `dangerouslyDisableSandbox?: bool` (+ internal field for simulated sed edits) | iff command proven read-only | = RO | 30k chars default (configurable 4k–128k; env up to 150k); spill ceiling 128k | no |
| PowerShell | same as Bash | as Bash | as Bash | as Bash | no (Windows only) |
| Read | `file_path: str` (absolute), `offset?: int`, `limit?: int`, `pages?: str` | yes | yes | unbounded by chars; self-limits by tokens | no |
| Write | `file_path`, `content` | no | no | 100k | no |
| Edit | `file_path`, `old_string`, `new_string`, `replace_all?: bool` | no | no | 100k | no |
| Glob | `pattern`, `path?` | yes | yes | 100k; at most 100 files | removed when shell `find` is shadowed |
| Grep | `pattern`, `path?`, `glob?`, `output_mode?` (`content`/`files_with_matches`/`count`), `-A`,`-B`,`-C`,`context`, `-n`, `-i`, `-o`, `type?`, `head_limit?` (default 250, 0 = unlimited), `offset?`, `multiline?` | yes | yes | 20k | removed when shell `grep` is shadowed |
| NotebookEdit | `notebook_path`, `cell_id?`, `new_source`, `cell_type?`, `edit_mode?` (replace/insert/delete) | no | no | 100k | yes |
| WebFetch | `url`, `prompt`, `offset?` | yes | — | 50k; exempt from batch budget | yes |
| WebSearch | `query` (min length 2), `allowed_domains?`, `blocked_domains?` | yes | — | 100k | yes |
| TodoWrite | `todos: [{content, status, activeForm}]` | no | no | 100k | yes |
| TaskCreate / TaskGet / TaskUpdate / TaskList | task-list v2 items | — | — | 100k | — |
| AskUserQuestion | `questions[1..4]: {question, header (≤12 chars), options[2..4]: {label, description, preview?}, multiSelect}` | yes | yes | 100k | — |
| EnterPlanMode / ExitPlanMode | `{}` / `{}` (a legacy allowed-prompts field is deprecated) | — | — | 100k | yes |
| Skill | `skill: str`, `args?: str` | — | — | 100k | — |
| Agent (Task) | subagent spec | — | — | own limit | not deferred when forking enabled |
| ToolSearch | `query: str`, `max_results?: int` (default 5) | yes | yes | 100k | never |
| LSP | `operation` (9 values), `filePath`, `line`, `character`, `query?` | yes | — | 100k | yes |
| Monitor | shell command or websocket source, `description`, `timeout_ms`, `persistent` | — | — | 10k | yes |
| GetTask | `taskId` | yes | — | 100k | — |
| TaskStop (KillShell, KillBash) | task id | no | — | 100k | — |
| Misc: SendUserMessage, SendUserFile, ScheduleWakeup, Cron*, EnterWorktree/ExitWorktree, SendMessage, ListMcpResources/ReadMcpResource, StructuredOutput, ReportFindings | various | — | — | — | mostly yes |

Backgrounding: Bash, Agent and a few others manage their own backgrounding ("self"); WebFetch and WebSearch may be detached ("detach"); everything else never backgrounds. Confidence: observed.

**Simple mode:** an environment switch (`CLAUDE_CODE_SIMPLE`) reduces the pool to Bash, Read, Edit (or a REPL tool + Edit + Write when REPL mode is on). Confidence: observed.

### 2.3 Read ledger entry

```
LedgerEntry:
  content: str | None        # verbatim only if ≤ 4096 bytes
  content_hash: str          # fast non-crypto hash of full content
  mtime: int                 # floored modification time at read/write
  offset: int | None
  limit: int | None
  partial_view: bool         # auto-paginated or explicitly ranged read
  not_in_model_context: bool # harness wrote content the model never saw in full
  seeded_from_context: bool  # e.g. memory/instruction files injected at startup
```

Ledger = LRU keyed by absolute path; caps 5000 entries and 25 MiB total. Subagents receive a clone with seeded entries removed. Confidence: observed.

### 2.4 Tool result envelope

A tool result is either content blocks (text / image / document / tool_reference) or an error wrapped in a `<tool_use_error>` tag. Error kinds: `InputValidationError` (bad JSON or schema), validation failure from `validate_input` (carries a numeric `errorCode` for metrics), permission denial, interrupt/cancel.

---

## 3. Behavioural requirements

### Pipeline
- **R1** MUST resolve the tool by name or alias; if the tool belongs to an MCP server still connecting, wait for it to appear before failing. Unknown tools get an immediate error result. Confidence: observed.
- **R2** If the input is not valid JSON, MUST return an InputValidationError that echoes the first bytes received and lists likely causes (unescaped backslashes, control characters, truncation). Confidence: observed.
- **R3** MUST run `coerce_input` before schema validation and SHOULD tell the model what was rewritten. Required coercions: Edit accepts `old_str`/`new_str`/`path` style names; Write accepts `file_text`/`file_content`/`text`/`body` as `content`; Grep maps `-l` and `-c` to `output_mode`, `file_path` to `path`, and drops `-r`. Confidence: observed.
- **R4** Schema failure MUST return InputValidationError plus the tool's steer text, plus a hint to load the schema via ToolSearch if the tool is deferred and its schema was never sent. Confidence: observed.
- **R5** `validate_input` failure MUST return its message as a tool error and record its `errorCode`. A validator MAY return "ask" to convert the failure into a permission prompt. Confidence: observed.
- **R6** After validation: PreToolUse hooks → permission pipeline → `call` → `map_result` → empty-result substitution → size budget/spill → PostToolUse hooks. Confidence: observed.
- **R7** A hook verdict of "defer" is only honoured in headless/print mode and only when the call is alone in its batch. Confidence: observed.
- **R8** An empty result MUST be replaced with a short note saying the named tool finished with no output; the model never sees an empty tool result. Confidence: observed.

### Scheduling
- **R9** Concurrency safety MUST be computed per call from the parsed input; an exception during the check means "not safe". Confidence: observed.
- **R10** Tools MUST be allowed to start while the assistant message is still streaming. Confidence: observed.
- **R11** Results MUST be emitted in the original tool-use order; progress messages MAY be flushed early. Confidence: observed.
- **R12** Context mutations requested by a serial tool (e.g. a skill narrowing allowed tools, changing model or effort) apply immediately; those from parallel-safe tools apply only when the whole parallel run finishes. Confidence: observed.
- **R13** Each call gets a child abort controller linked to the turn. Interrupts, streaming fallback discards and conversation-ended cancellations produce synthetic error results so every tool_use has a matching result. Confidence: observed.
- **R14** Non-streaming execution SHOULD partition calls into runs of consecutive safe calls, run each safe run with bounded parallelism (default 10, env `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`), and run unsafe calls singly. Confidence: observed.
- **R15** A PostToolBatch hook SHOULD fire once per batch. Confidence: observed.
- **R16** The system prompt SHOULD encourage issuing independent calls together. Confidence: observed.

### File state
- **R17** Edit and Write on an existing file MUST fail unless the ledger has a non-partial entry for it (new files and empty-`old_string` creates are exempt). Confidence: observed.
- **R18** An mtime newer than the ledger is NOT by itself stale: if the prior read was a full read and the content hash still matches, proceed. Confidence: observed.
- **R19** If the content really changed but `old_string` still matches uniquely and the path is auto-allowed by permissions, the edit SHOULD apply ("stale-recovered") and the result MUST warn that the file contains changes not in the model's context. Otherwise fail with a message suggesting a formatter may have rewritten the file and instructing a fresh Read. Confidence: observed.
- **R20** After a successful write, update the ledger with new content/hash/mtime; set `not_in_model_context` when appropriate. Edit/Write results SHOULD tell the model its file view is current and re-reading is unnecessary. Confidence: observed.
- **R21** Read of the same path with the same range and unchanged mtime MUST return a "file unchanged" stub telling the model to use the earlier result. A read of a file already injected at startup returns a pointer to that injected copy. A kill switch SHOULD exist. Confidence: observed.
- **R22** After a shell command that looks like a formatter/fixer, re-stat every ledger path; for files whose mtime moved, append a note naming up to 5 of them and advising a Read before editing. Confidence: observed.

### Output size
- **R23** Per-tool spill threshold = `min(max_result_chars, ceiling)` (ceiling 50k default; 128k for Bash); remotely overridable per tool. Image and document blocks are exempt. Confidence: observed.
- **R24** Over threshold, write the full output to `<project-state-dir>/<session>/tool-results/<tool_use_id>.(txt|json)` using exclusive create that refuses symlinks/hardlinks (shell output capped at 64 MiB), and replace the result with a `<persisted-output>` block containing the size, the saved path and a ~2 KB preview cut back to a newline when that keeps ≥50% of the preview. If saving fails, return a `<truncated-output>` block explaining that only the head is shown and suggesting paging/filtering. Confidence: observed.
- **R25** Per-message aggregate budget: if all tool results in one user message exceed 200,000 chars, spill the largest fresh results until under budget. Replacement decisions MUST be memoised by tool_use_id and re-applied identically on later turns (prompt-cache stability). Confidence: observed.
- **R26** Bash applies its own head truncation (default 30k chars) with a "N lines truncated" marker before the generic spill logic. Confidence: observed.

### Edit
- **R27** Matching MUST follow the cascade in §4.3; the replacement MUST be re-expressed in the file's own quoting/escaping style. Confidence: observed.
- **R28** For non-Markdown files strip trailing whitespace from `new_string` lines. Confidence: observed.
- **R29** If not found, retry after reversing known API-side tag sanitizations (short placeholder tags mapped back to the real ones), applying the same mapping to `new_string`. Confidence: observed.
- **R30** When `new_string` is empty and `old_string` lacks a trailing newline but `old_string + "\n"` exists, delete the newline too. Confidence: observed.
- **R31** Not-found → error quoting the attempted string (noting if escape swapping was tried). Multiple matches without `replace_all` → error stating the count (errorCode 9). Confidence: observed.
- **R32** Multi-edit (internal list form) applies sequentially; reject if an `old_string` is contained in an earlier `new_string`, or if the final content equals the original. Confidence: observed.
- **R33** Max editable size 1 GiB; `.ipynb` MUST go through NotebookEdit; preserve BOM, encoding (UTF‑8 / UTF‑16LE) and line endings. Confidence: observed.
- **R34** A subagent attempting to Write a Markdown file whose name begins with REPORT, SUMMARY, FINDINGS or ANALYSIS MUST be refused with guidance to return the content as text. Confidence: observed.

### Search
- **R35** Grep is ripgrep-backed: include hidden files, exclude VCS directories (`.git .svn .hg .bzr .jj .sl`), max column 500; use `-e` when pattern begins with `-`; keep brace globs intact while splitting comma/space lists; paginate with `head_limit`/`offset` and add a footer noting the limit applied; push read-deny permission rules into ignore-globs and re-filter results. Confidence: observed.
- **R36** Glob returns ≤100 paths sorted by mtime with a footer giving exact or lower-bound totals. Confidence: observed.
- **R37** On native builds, Glob and Grep SHOULD be removed and the shell snapshot instead shadows `find` with a bundled `bfs` (DFS, findutils regex dialect), `grep` with a bundled `ugrep` (basic regex, honour ignore files, hidden, skip binary, exclude `.git`; refuse config-writing and NUL-output flags), and `rg` with the bundled ripgrep. The Bash guidance then drops "don't use grep/find" and adds advice to search from `.` and a regex alternation gotcha. Confidence: observed.

### Shell
- **R38** No persistent shell process; each command runs in a fresh `-c` shell that sources the session snapshot (login shell only if no snapshot). See §4.5. Confidence: observed.
- **R39** Working directory persists across calls; shell variables do not. The new cwd is read back from a temp file and validated (no dot segments, no device or network paths). If it lands outside allowed directories, reset it and say so in the result. Subagents MAY pin cwd. Confidence: observed.
- **R40** Timeout default 120,000 ms, maximum 600,000 ms (env `BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS`). On timeout the main agent's command is moved to the background (not killed) and the result gives the task id and output file (env `CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS`). Confidence: observed.
- **R41** `run_in_background` streams output to `<tmp>/<session>/tasks/<id>`; the model is told it will be notified on completion and can Read the file for interim output. Users can background a running command with a keystroke. GetTask/TaskStop manage tasks. Confidence: observed.
- **R42** A foreground command that begins with `sleep N` where N ≥ 25 s MUST be rejected, pointing at background execution or Monitor and forbidding chained shorter sleeps. Confidence: observed.
- **R43** Bash `is_read_only` parses the command against a read-only allowlist (doc 04 §4.3); `is_concurrency_safe = is_read_only`. Confidence: observed.
- **R44** Reject commands containing control characters (they could be hidden in the approval UI). Confidence: observed.
- **R45** In-place `sed` edits SHOULD be previewed as a diff and applied by the harness with a base-hash check. Confidence: observed.
- **R46** Stdout that is a data-URI image becomes an image block. Results MAY carry a rate-limit hint for `gh` and a working-tree diff for hooks/UI. `CLAUDE_CODE_SHELL_PREFIX` wraps every command. Confidence: observed.
- **R47** The `description` input exists for the human approver and SHOULD be guided to describe intent without echoing the command, flags or paths. Confidence: observed.

### Read
- **R48** Text: default 2000 lines, output `N<TAB>line` (switch separator to `:` when lines start with a tab). Limits: 256 KiB raw and 25,000 tokens (env `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`), with a cheap bytes/128 pre-estimate before real counting. Confidence: observed.
- **R49** A whole-file read over the token cap auto-paginates (§4.6), marks `partial_view`, and appends a banner giving the shown range, total lines, the next offset to use, and a warning not to answer from the first page alone. An explicit range over the cap is an error. Confidence: observed.
- **R50** Refuse known binary extensions (suggest a skill), `/dev/stdin` and FIFOs. Missing file → "did you mean" suggestion plus cwd. Empty file or offset past EOF → system-reminder warning instead of content. Confidence: observed.
- **R51** Images (PNG/JPEG/GIF/WebP, detected by magic bytes): downscale to ≤2000×2000 and ≤5 MiB base64; token estimate `ceil(w/28)*ceil(h/28)`; compress further if over budget; last resort 400×400 JPEG at quality 20. Attach a note with original vs displayed dimensions. Extension/content mismatch → hint that a download probably saved an error or login page. Confidence: observed.
- **R52** PDF: `pages` accepts a single `N` or `N-M` range, ≤20 pages per call; PDFs over 10 pages require `pages`. Pages rendered to JPEG; small PDFs MAY be sent as a document block. Disabled for older model families. Confidence: observed.
- **R53** Notebooks are returned as cells (with outputs), each tagged with its cell id. Confidence: observed.

### Deferral & ToolSearch
- **R54** A tool is deferred if it is MCP, sets `should_defer`, or is in a remote list; `always_load` cancels. Never deferred: ToolSearch, StructuredOutput, Agent (when forking), the user-message tool, a configurable list of built-ins, worktree entry in background sessions. Confidence: observed.
- **R55** Deferred tools are announced by name in a system-reminder attachment that explains schemas are not loaded and how to select them; delta announcements are sent when tools appear, change or disappear. Confidence: observed.
- **R56** ToolSearch supports `select:A,B` (≤64 names), free text (≤32 words, ≤16 KB), and `+term` required terms; scoring per §4.7; if nothing matches and MCP servers are pending, poll up to 5 s at 50 ms. Result is a list of `tool_reference` blocks which the API expands into schemas. Zero-match text explains pending/failed/blocked servers and marks any quoted server error text as untrusted. Confidence: observed.

### Other tools
- **R57** TodoWrite replaces the whole list each call (keyed per agent); when every item is completed the list is cleared; result nudges continued use. Confidence: observed.
- **R58** AskUserQuestion enforces unique question texts and option labels, auto-adds an "Other" choice, returns an away-from-keyboard message after a timeout telling the model to proceed on judgment, and when given fewer than 2 options steers the model to state the single path and continue instead of inventing a filler. Hosts MAY support free-text and numeric kinds. Confidence: observed.
- **R59** EnterPlanMode/ExitPlanMode are main-agent only. Entering switches permission mode to `plan` and tells the model not to edit yet. Exiting reads the plan from a plan file on disk (injected into input by the harness) and requires user interaction. Confidence: observed.
- **R60** Skill validates the name against the catalogue with edit-distance ≤2 suggestions; runs inline (returning new messages + context layers: allowed tools, model, effort) or forked as a subagent; fork recursion blocked. Confidence: observed.
- **R61** WebFetch upgrades to HTTPS, refuses dotless hosts, does not follow cross-host redirects (reports them), converts to Markdown, answers `prompt` with a small fast model, caches 15 min (`CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS`), paginates with `offset`, wraps content in an untrusted-content tag. Confidence: observed.
- **R62** WebSearch is a nested API call using the server-side web search tool with at most 8 uses; guidance requires a sources section and awareness of the current year. Confidence: observed.
- **R63** Monitor emits one notification per stdout line or per websocket frame (≤1 MiB), with SSRF guards against private, link-local and cloud-metadata addresses; guidance covers line buffering and that no output does not mean success. Confidence: observed.
- **R64** LSP offers 9 operations (definition, references, hover, document symbols, workspace symbols, implementation, prepare call hierarchy, incoming calls, outgoing calls) with 1-based positions; Edit/Write notify the language server of changes and saves. Confidence: observed.
- **R65** Background/system notifications injected into context MUST be labelled as not user input and must escape any closing system-reminder tags in their content. Confidence: observed.

### Prompts
- **R66** Each tool provides a lean and a classic prompt; the classic (longer, example-heavy) variant is chosen for older/smaller model families and the lean one for newer models, with remote override. Individual sentences MAY be flag-gated for A/B tests. Confidence: observed.

---

## 4. Algorithms

### 4.1 Streaming concurrency scheduler

```python
class StreamingExecutor:
    def __init__(self):
        self.queue = []          # in arrival order
        self.running = []

    def add(self, tool_use):
        tool = registry.lookup(tool_use.name)
        if tool is None:
            self.queue.append(Done(tool_use, error("no such tool")))
            return
        try:
            parsed = tool.input_schema.parse(tool.coerce(tool_use.input))
            safe = tool.is_concurrency_safe(parsed)
        except Exception:
            safe = False
        self.queue.append(Pending(tool_use, safe))
        self.pump()

    def can_start(self, safe):
        return not self.running or (safe and all(r.safe for r in self.running))

    def pump(self):
        for item in self.queue:
            if item.state != "pending":
                continue
            if self.can_start(item.safe):
                self.start(item)            # async; on finish -> pump()
            elif not item.safe:
                break                       # serial call blocks everything after it
            # a safe call that can't start (serial running) also waits; loop continues
            # but can_start will be False for all subsequent items

    def completed_in_order(self):
        for item in self.queue:
            yield from item.flush_progress()
            if item.state == "done":
                if not item.emitted:
                    item.emitted = True
                    yield item.result
            elif not item.safe:
                return                      # don't pass an unfinished serial call
            else:
                return                      # preserve original order

    def on_finish(self, item):
        if not item.safe:
            apply_context_layers(item)      # immediately
        elif not any(r.safe for r in self.running):
            apply_deferred_layers_of_run()  # when the parallel run ends
        self.pump()

    def discard_all(self, reason):
        for item in self.queue:
            item.abort()
            item.result = synthetic_error(reason)
```

### 4.2 Read ledger & staleness check (Edit/Write)

```python
def check_before_write(path, old_string, is_new_file):
    e = ledger.get(path)
    if is_new_file or old_string == "":
        return OK
    if e is None or e.partial_view:
        return fail("read the file first", code=EDIT_NOT_READ)
    disk_mtime = floor(stat(path).mtime)
    if disk_mtime <= e.mtime:
        return OK
    current = read(path)
    if was_full_read(e) and hash(current) == e.content_hash:
        return OK                           # touched / formatter wrote identical bytes
    if count_matches(current, old_string) == 1 and permissions.auto_allows(path):
        return OK_STALE_RECOVERED           # apply, but warn the model
    return fail("file changed since last read; Read again then retry")

def was_full_read(e):
    return (e.offset in (None, 1)) and not e.partial_view and not e.hit_limit

def after_write(path, new_content, model_saw_full):
    ledger.put(path, LedgerEntry(
        content=new_content if len(new_content.encode()) <= 4096 else None,
        content_hash=hash(new_content), mtime=floor(stat(path).mtime),
        not_in_model_context=not model_saw_full))

def after_shell(command, result):
    if not FORMATTER_PATTERN.search(command):   # --write, --fix, --in-place, black, isort,
        return                                  # ruff format, cargo fmt, rustfmt, go fmt, prettier...
    changed = [p for p, e in ledger.items() if floor(stat(p).mtime) > e.mtime]
    if changed:
        result.append_note(f"{len(changed)} previously-read files modified: "
                           + ", ".join(changed[:5]) + "; Read before editing.")

def read_dedup(path, offset, limit):
    e = ledger.get(path)
    if e and e.offset == offset and e.limit == limit and floor(stat(path).mtime) == e.mtime:
        return "unchanged-stub" if not e.seeded_from_context else "already-in-context-stub"
    return None
```

### 4.3 Edit string matching

```python
SANITIZE_MAP = {...}   # short placeholder tags -> original tag names (API-side sanitization)

def find_actual(file, old):
    if old in file:
        return old
    f2, o2 = fold_quotes(file), fold_quotes(old)        # curly ' " -> straight
    i = f2.find(o2)
    if i >= 0:
        return file[i:i+len(old)]                       # return the file's real substring
    if has_unicode_escapes(old):
        dec = decode_escapes(old)
        if dec in file:
            return dec
    if has_non_ascii(old) and "\\u" in file:
        m = escaped_regex_search(file, old)             # respects backslash parity
        if m:
            return m
    return None

def restyle_new(new, actual_old, original_old):
    if actual_old != original_old and uses_curly_quotes(actual_old):
        new = recurl(new)
        # " -> opening after whitespace/([{/dash, closing otherwise
        # ' between two letters -> apostrophe glyph
    if matched_via_escape(actual_old):
        new = escape_non_ascii(new, hex_case=detect_hex_case(actual_old))
    return new

def apply_edit(path, old, new, replace_all):
    file = read(path)
    if not is_markdown(path):
        new = strip_trailing_ws_per_line(new)
    actual = find_actual(file, old)
    if actual is None:
        old2, new2 = desanitize(old), desanitize(new)
        actual, new = find_actual(file, old2), new2
    if actual is None:
        return fail("not found", quote=old)
    n = file.count(actual)
    if n > 1 and not replace_all:
        return fail(f"{n} matches; set replace_all or add context", code=9)
    new = restyle_new(new, actual, old)
    if new == "" and not actual.endswith("\n") and (actual + "\n") in file:
        actual += "\n"
    out = file.replace(actual, new) if replace_all else file.replace(actual, new, 1)
    write_preserving(path, out, encoding=detect(path), eol=detect_eol(file), bom=has_bom(path))
```

### 4.4 Output spill

```python
CEILING = 50_000; BATCH_BUDGET = 200_000; PREVIEW = 2000

def finalize(tool, result, tool_use_id):
    if is_empty(result):
        return text(f"({tool.name} completed with no output)")
    if result.is_media():
        return result
    limit = min(tool.max_result_chars, tool.persistence_ceiling or CEILING)
    limit = remote_override(tool.name, limit)
    s = serialize(result)
    if len(s) <= limit:
        return result
    return spill(s, tool_use_id)

def spill(s, tool_use_id):
    path = state_dir / session / "tool-results" / f"{tool_use_id}.{ext}"
    try:
        exclusive_create_no_links(path, s)
    except OSError:
        return text(f"<truncated-output>head of {len(s)} chars only; rest dropped; "
                    "page or filter if possible\n{s[:PREVIEW]}</truncated-output>")
    head = s[:PREVIEW]
    nl = head.rfind("\n")
    if nl > PREVIEW // 2:
        head = head[:nl]
    return text(f"<persisted-output>\nsize {human(len(s))}; saved to {path}\n"
                f"preview:\n{head}\n...\n</persisted-output>")

memo = {}   # tool_use_id -> replacement (persisted across turns)

def enforce_batch_budget(results):
    for r in results:
        if r.id in memo:
            r.content = memo[r.id]
    total = sum(size(r) for r in results if not r.tool.skip_batch_budget)
    for r in sorted(fresh(results), key=size, reverse=True):
        if total <= BATCH_BUDGET:
            break
        old = size(r)
        r.content = spill(serialize(r), r.id)
        memo[r.id] = r.content
        total -= old - size(r)
```

### 4.5 Shell snapshot execution

```python
def capture_snapshot(shell):            # once per session
    script = []
    for fn in user_functions(shell):
        script.append(f"eval {quote(fn.source)} 2>/dev/null")   # one per function: a bad one only drops itself
    script += shopt_state(shell) + aliases(shell) + [f"export PATH={quote(path)}"]
    script += shadow("rg", BUNDLED_RG)
    if native_build:
        script += shadow("find", BUNDLED_BFS, "-S dfs -regextype findutils-default")
        script += shadow("grep", BUNDLED_UGREP, "-G --ignore-files --hidden -I --exclude-dir=.git",
                         refuse=["--config", "--save-config", "-Z", "--null"])
    script += pkill_guard(harness_pid)   # refuse patterns matching the harness itself
    write(snapshot_path, script)

def run(cmd, timeout_ms=120_000):
    reject_if(has_control_chars(cmd))
    reject_if(leading_sleep_seconds(cmd) >= 25 and not background)
    cwd_file = tmp / f"harness-{id}-cwd"
    parts = []
    if exists(snapshot_path):
        parts.append(f"source {quote(snapshot_path)}")
        login = False
    else:
        login = True
    parts += tmpdir_exports() + git_env_exports() + ["shopt -u extglob", unalias_guards()]
    parts.append(f"eval {quote(cmd)}")
    parts.append(f"pwd -P >| {quote(cwd_file)}")
    full = " && ".join(parts)
    if prefix := env.get("CLAUDE_CODE_SHELL_PREFIX"):
        full = wrap(prefix, full)
    proc = spawn([user_shell] + (["-l"] if login else []) + ["-c", full],
                 cwd=state.cwd, sandbox=sandbox_profile_for(cmd))
    if not proc.wait(min(timeout_ms, 600_000)):
        if is_main_agent:
            task = move_to_background(proc)           # keep running, stream to file
            return note(f"moved to background as {task.id}; output at {task.file}")
        proc.kill()
    new_cwd = read(cwd_file)
    if valid_path(new_cwd) and within_allowed_dirs(new_cwd) and not pinned_cwd:
        state.cwd = new_cwd
    elif new_cwd != state.cwd:
        state.cwd = original_cwd; result.note("cwd was reset")
    out = head_truncate(proc.output, bash_max_chars())   # "... [N lines truncated] ..."
    after_shell(cmd, out)
    return out
```

### 4.6 Read auto-pagination

```python
def read_text(path, offset=None, limit=None):
    lines = read_lines(path)
    explicit = offset is not None or limit is not None
    sel = lines[(offset or 1) - 1 : (offset or 1) - 1 + (limit or 2000)]
    body = format_numbered(sel)
    if len(body.encode()) / 128 <= MAX_TOKENS and count_tokens(body) <= MAX_TOKENS:
        return body
    if explicit:
        raise MaxTokensExceeded
    keep = int(len(sel) * MAX_TOKENS / count_tokens(body) * 0.85)
    for _ in range(6):
        body = format_numbered(sel[:keep])
        if count_tokens(body) <= MAX_TOKENS:
            break
        keep = int(keep * 0.7)
    mark_partial(path)
    return body + banner(shown=(1, keep), total=len(lines), next_offset=keep + 1)
```

### 4.7 ToolSearch scoring

```python
W = {   # (builtin, mcp)
  "part_exact": (10, 12), "part_substr": (5, 6),
  "full_exact": (10, 12), "full_substr": (3, 4),
  "hint_word": (4, 4),    "prompt_word": (2, 2)}

def search(query, tools, max_results=5):
    if query.startswith("select:"):
        names = query[7:].split(",")[:64]
        return [t for t in tools if t.name in names]
    terms = query.split()[:32]
    required = [t[1:] for t in terms if t.startswith("+")]
    terms = [t.lstrip("+").lower() for t in terms]
    for t in tools:
        if t.name.lower() == query.lower():
            return [t]
    scored = []
    for t in tools:
        parts = split_name(t.name)    # camelCase, "_" and "__"
        if any(not matches(r, t) for r in required):
            continue
        k = 1 if t.is_mcp else 0
        s = 0
        for q in terms:
            if q in parts:                          s += W["part_exact"][k]
            elif any(q in p for p in parts):        s += W["part_substr"][k]
            if q == t.name.lower():                 s += W["full_exact"][k]
            elif q in t.name.lower():               s += W["full_substr"][k]
            if word_boundary(q, t.search_hint):     s += W["hint_word"][k]
            if word_boundary(q, t.prompt_text):     s += W["prompt_word"][k]
        if s > 0:
            scored.append((s, t))
    if not scored and mcp_pending():
        poll_until(lambda: rerun_nonempty(), timeout=5.0, step=0.05)
    return [t for _, t in sorted(scored, reverse=True)[:max_results]]
```

---

## 5. Constants & knobs

| Name | Value |
|---|---|
| Default per-tool spill ceiling | 50,000 chars |
| Bash spill ceiling | 128,000 chars |
| Bash head truncation | 30,000 chars default; configurable 4k–128k; `BASH_MAX_OUTPUT_LENGTH` up to 150k |
| Per-message aggregate budget | 200,000 chars |
| Spill preview | 2,000 bytes |
| Shell output on-disk cap | 64 MiB |
| Chars per token estimate | 4 |
| Other size constants seen | 400,000; 500,000 (purpose not resolved) |
| Ledger LRU | 5000 entries, 25 MiB; verbatim content ≤ 4096 bytes |
| Read defaults | 2000 lines; 256 KiB; 25,000 tokens (`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`); bytes/token pre-check 128 |
| Read pagination | ×0.85 initial, ×0.7 shrink, ≤6 iterations |
| Image | ≤2000×2000 px, ≤5 MiB base64, 28-px token tiles, fallback 400×400 JPEG q20 |
| PDF | ≤20 pages/call; `pages` required over 10 pages |
| Edit max file size | 1 GiB |
| Grep `head_limit` | 250 (0 = unlimited); max columns 500 |
| Glob results | 100 |
| Bash timeout | default 120,000 ms (`BASH_DEFAULT_TIMEOUT_MS`), max 600,000 ms (`BASH_MAX_TIMEOUT_MS`) |
| Auto-background on timeout | `CLAUDE_CODE_AUTO_BACKGROUND_TIMEOUT_MS` |
| Blocked leading sleep | ≥ 25 s |
| Parallel tool cap (non-streaming) | 10 (`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`) |
| ToolSearch | default 5 results; select ≤64 names; ≤32 words; ≤16 KB; MCP wait 5 s @ 50 ms |
| WebFetch cache | 15 min (`CLAUDE_CODE_WEBFETCH_CACHE_TTL_MS`); result 50k |
| WebSearch | ≤8 searches per call |
| Monitor | ≤1 MiB per websocket frame; result 10k |
| Formatter-change note | ≤5 file names |
| Simple mode | `CLAUDE_CODE_SIMPLE` |
| Shell wrapper | `CLAUDE_CODE_SHELL_PREFIX` |

---

## 6. Rationale

- **Per-input concurrency** lets the cheapest common case (several reads, `ls`, `git status`) fan out while anything that writes stays serial; ordering results and blocking behind serial calls eliminates write/read races without a dependency analyser.
- **Streaming execution** hides tool latency behind token generation; the discard path keeps transcripts well-formed when a stream is retried.
- **Content-aware staleness** avoids the classic loop where a formatter touches a file and every subsequent edit fails; the stale-recovered path keeps safety because uniqueness of `old_string` plus a warning bounds the risk.
- **Spill-to-disk instead of truncation** preserves information (the model can page the file) while protecting context; memoised replacements keep prompt-cache prefixes byte-identical.
- **Forgiving matching with style restoration** absorbs tokenizer/rendering artefacts (curly quotes, escape sequences, sanitised tags) without corrupting the file's own conventions.
- **Snapshot-based shell** gives cwd persistence and user environment without long-lived process state that can wedge, and provides a single place to shadow binaries for safety and speed.
- **Auto-backgrounding and sleep blocking** redirect the model from wasteful polling patterns to event-driven primitives.
- **Deferred tools** keep the system prompt small when dozens of MCP tools are attached, at the cost of one search round trip.
- **Model-specific prompt variants** spend coaching tokens only where the model needs them.

---

## 7. Harness design takeaways

- **T1** Decide whether a call can run in parallel from its parsed input, not from the tool type. A shell call is parallel-safe exactly when its command is provably read-only. Pair this with an ordered queue in which a serial call blocks later ones and results come back in issue order: most of the speedup, none of the write races.
- **T2** Begin running tools while the model is still streaming its message, and keep a discard path so that a retried or fallback stream can replace half-finished calls with synthetic errors.
- **T3** Maintain a ledger of what the model has read, with mtime and a content hash (full text only for small files). When mtime moves, compare hashes before rejecting; if the file truly changed but the edit still applies uniquely, allow it and tell the model plainly. This kills most formatter/`touch` induced retry loops while staying safe.
- **T4** Feed side effects back into the ledger: after formatter-like shell commands, re-stat previously read files and tell the model which ones changed, rather than invalidating everything.
- **T5** Treat redundant calls as mistakes and say so: a re-read of an unchanged file gets a short "use the earlier result" stub, and the prompt warns against re-reading after edits.
- **T6** Be lenient when matching and then restore the file's own form: fold curly quotes and escape sequences to match, re-style the replacement to match the file, strip trailing whitespace, undo API tag sanitisation, and accept other harnesses' field names via input coercion while reporting what was changed.
- **T7** Give every tool a size budget and every batch an aggregate budget, and spill to disk rather than truncate: a saved-file pointer plus a short preview lets the model page the rest; spill the largest results first and memoise replacements so the cached prompt prefix never changes.
- **T8** Hide the long tail of tools behind a search tool: announce names only, load schemas on demand as references, rank name parts above descriptions with a small boost for MCP tools, support exact `select:` lookups, wait briefly for MCP servers still connecting, and mention the missing schema inside the validation error when a deferred tool is called blind.
- **T9** Don't keep a persistent shell; snapshot it. Run each command in a fresh shell that sources the captured functions/aliases/PATH, evaluates the command and records the final cwd to a file: cwd persistence without leaked state, natural crash recovery, and a hook for shadowing binaries (bundled search tools; a process-kill wrapper that cannot target the harness).
- **T10** On timeout, background the command instead of killing it: stream to a file, notify on completion, and block long leading sleeps with a pointer to the proper primitive (background run or a monitor). This removes the commonest agent anti-pattern, sleep-polling.
- **T11** Make error messages carry the recovery step ("did you mean…", "use 0 for unlimited", "Read the file to refresh") and attach a numeric error code so you can measure which recoveries work.
- **T12** Version tool prompts by model: a lean variant for strong models, a verbose example-rich variant for older ones, with individual sentences behind flags so wording can be A/B tested like code.

---

## 8. Uncertainties

- The exact read-only Bash allowlist used for concurrency is described in doc 04 §4.3 but was not fully enumerated (inferred completeness).
- How the API expands `tool_reference` blocks and whether a "strict" tool flag maps to API strict schemas are server-side and inferred.
- Agent/subagent, task-list v2, cron, worktree and inter-agent messaging tools were inventoried but not analysed.
- PDF rendering backend (system utilities vs built-in) and the threshold for sending PDFs as document blocks were not resolved.
- Multi-machine routing (`_host`) semantics beyond input shape were only skimmed.
- Purpose of the 400,000 and 500,000 char constants is unknown.
- The desktop host appears to add its own MCP tools but not to redefine core tools (inferred).
