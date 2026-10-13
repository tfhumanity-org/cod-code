# 00 — Desktop host: process architecture (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

## 1. Purpose & scope

This document specifies the process architecture of a desktop application that hosts an agentic coding engine. It covers the window/view model, inter-process messaging rules, how the engine binary is installed and launched, session persistence, git worktree management, the embedded preview browser, the terminal, sandboxed helper processes, and runtime hardening. It does not cover the agent loop itself (see spec 01) or prompt assembly (see spec 02).

Out of scope: renderer UI layout, native add-on internals, the browser-extension messaging format and the task scheduler loop. None of these were analysed.

## 2. Concepts & data model

**Host app.** A desktop shell built on Electron. Its main window contains one embedded web view that loads the product's web front-end. A coding-session route inside that web app provides the coding UI.

**Engine.** A standalone command-line agent binary. The host never reimplements the agent loop. It launches the engine as a child process through the Agent SDK and talks to it over a bidirectional JSON stream (see 01 §R12).

**Engine install record** (directory layout, own naming):
```
<userData>/<engine-dir>/<engine-version>/<content-hash-prefix>/<binary>
   + a marker file recording that the hash was verified
```
Several versions can sit side by side. A separate variant directory holds an engine build for use inside a VM.

**Session record** (one JSON file per session):
```
<userData>/<sessions-dir>/<account-id>/<org-id>/local_<uuid>.json
{
  engine_session_id: str     # used to resume the engine conversation
  cwd: path
  worktree?: WorktreeRef
  title, created_at, ...     # UI metadata
}
```

**WorktreeRef**:
```
path:   <repo>/.claude/worktrees/<slug>-<6 hex chars>
branch: <prefix>/<slug>      # prefix comes from a setting, default "claude"
```
A JSON registry file in host storage lists all worktrees. A warm pool of pre-created worktrees may also be kept.

**Utility processes** (each a separate OS process):

| Role | Responsibility |
|---|---|
| pty host | spawn, write, resize, kill and scrollback for terminal PTYs |
| heavy work | git operations, headless terminal rendering, statistics |
| file index | fuzzy file search for @-mentions |
| transcript search | full-text search over past sessions |
| MCP runtime | hosts MCP servers directly |
| shell path | resolves the user's login-shell environment |
| stall sampler | profiles main-thread stalls |

**Helpers**: a computer-use helper, a browser native-messaging host, an iOS-simulator sidecar, a permission fixer, a helper that disclaims inherited OS privacy permissions for child processes, and an optional Linux VM.

**In-process MCP servers.** The host exposes its capabilities to the engine as SDK MCP servers. They cover the session, session management, pull requests, views and panes, host utilities, the sidebar, settings, windows, directories, connectors, the terminal, the preview browser, computer use, scheduled tasks and a connector registry.

## 3. Behavioural requirements

- **R1 Web front-end as GUI.** The main window MUST load the product web app into an embedded view and select the coding route when the sidebar is in coding mode. For third-party-inference deployments, a bundled copy of the front-end MUST be served from a custom app-scheme origin, with stubbed bootstrap endpoints. *Confidence: observed.*
- **R2 IPC origin validation.** Every privileged IPC handler MUST check that the sender is the top-level frame and that its origin is in an allowlist (the product domains plus the bundled app-scheme origin). Channel names SHOULD be generated per interface and method, and payloads MUST be validated against typed schemas. Preload scripts expose namespaced binding objects (settings, web, skills, simulator and so on). *Confidence: observed.*
- **R3 Pinned engine.** The host MUST download a specific engine version, store it under a path keyed by version and content hash, and verify it before use. A developer-only env var (`CLAUDE_CODE_LOCAL_BINARY`) MAY override the binary. *Confidence: observed.*
- **R4 Engine spawn environment.** When launching the engine, the host MUST set:
  - `CLAUDE_CODE_ENTRYPOINT=claude-desktop`;
  - OAuth credentials, with `ANTHROPIC_API_KEY` set to empty;
  - non-blocking MCP connection (`MCP_CONNECTION_NONBLOCKING`);
  - micro-compaction disabled (`DISABLE_MICROCOMPACT=1`);
  - system CA use (`NODE_USE_SYSTEM_CA=1`);
  - opt-in to the ask-user-question tool.

  Per session, it MUST set `BROWSER` to the shim (R7) and pass a bridge descriptor `{socket, key}` in `CLAUDE_BROWSER_BRIDGE`. *Confidence: observed.*
- **R5 Resume and rewind.** To resume a session, the host passes the engine's session id. To rewind, it resumes at a chosen message and forks into a new session. *Confidence: observed.*
- **R6 Worktree creation.** Creation MUST run `git worktree add` with no checkout, no upstream tracking and a new branch. The checkout follows later as a deferred step so the UI does not wait. Paths and branches follow §2. *Confidence: observed.*
- **R7 Browser shim.** `$BROWSER` MUST point to a small script, which may be a shell/Node polyglot. The script sends `{v:1, key, url}` over a unix socket with mode 0600 and receives either "shown in pane" or "declined". The host MUST accept only loopback URLs on ports owned by the session. *Confidence: observed.*
- **R8 Preview pane.** Each preview tab MUST be a separate embedded view with its own storage partition. Automation MUST go through the DevTools protocol:

  | Operation | DevTools domain used |
  |---|---|
  | page reading and element find | accessibility tree |
  | clicks and typing | input events |
  | script execution | runtime evaluation |
  | viewport resize | device-metrics emulation |

  Dev servers listed in `.claude/launch.json` are started with a `PORT` env var. Navigation to a new origin needs per-origin approval, and origins are classified (high-risk, financial, private network). *Confidence: observed.*
- **R9 Terminal.** PTYs MUST live in the pty-host process. Shell-integration marks (OSC 133) MUST carry a per-session nonce so that program output cannot spoof them. Scrollback persisted to disk MUST be redacted. A tool that reads the terminal SHOULD render the screen with a headless terminal emulator in the heavy-work process. *Confidence: observed (rendering location inferred).*
- **R10 iOS simulator sidecar.** The sidecar MUST communicate over stdio JSON-RPC and run under a deny-by-default sandbox profile: no network and no home-directory access. It may run only `xcrun`, `xcodebuild` and `plutil`. *Confidence: observed.*
- **R11 VM sandbox (optional).** A Linux VM MAY be run on the OS virtualization framework. It boots from a read-only helper disk containing a guest daemon plus a downloaded root filesystem of about 1.2 GB. Networking goes through a user-space network stack with an egress-inspecting proxy. *Confidence: observed.*
- **R12 Runtime hardening.** Electron fuses MUST be set as follows:
  - off: run-as-node, `NODE_OPTIONS`, inspector flags;
  - on: archive integrity, load-only-from-archive.

  A V8 compile cache MAY be kept. *Confidence: observed (fuse order inferred).*
- **R13 Managed configuration.** The host MUST read enterprise policy from the platform mechanisms: macOS managed preferences, Windows group policy, and `/etc/claude-desktop/managed-settings.json` on Linux. The documented key set has about 180 keys. *Confidence: observed.*
- **R14 Updates and telemetry.** The host SHOULD auto-update from a JSON feed for each platform and architecture. Crash reporting MUST exclude default PII and MUST be disabled for regulated-health organisations. *Confidence: observed.*

## 4. Algorithms

```python
def launch_engine(session):
    binary = os.environ.get("CLAUDE_CODE_LOCAL_BINARY") or installed_engine(PINNED_VERSION)
    assert is_verified(binary)
    env = base_env() | {
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
        "ANTHROPIC_API_KEY": "",
        "MCP_CONNECTION_NONBLOCKING": "1",
        "DISABLE_MICROCOMPACT": "1",
        "NODE_USE_SYSTEM_CA": "1",
        "BROWSER": SHIM_PATH,
        "CLAUDE_BROWSER_BRIDGE": json.dumps({"socket": session.bridge_sock, "key": session.bridge_key}),
        **oauth_env(session.account),
    }
    return sdk_query(binary, env, resume=session.engine_session_id, mcp_servers=host_mcp_servers(session))

def on_shim_request(msg, session):
    if msg.v != 1 or not constant_time_eq(msg.key, session.bridge_key):
        return "declined"
    u = urlparse(msg.url)
    if u.hostname not in LOOPBACK or u.port not in session.owned_ports:
        return "declined"
    open_in_preview_pane(session, msg.url)
    return "pane"

def ipc_guard(event, handler, payload):
    frame = event.sender_frame
    if not frame.is_top_level or origin(frame.url) not in ORIGIN_ALLOWLIST:
        raise PermissionError
    return handler(validate(schema_for(handler), payload))

def create_worktree(repo, slug, prefix=setting("branch prefix") or "claude"):
    path = f"{repo}/.claude/worktrees/{slug}-{random_hex(6)}"
    branch = f"{prefix}/{slug}"
    git(repo, "worktree", "add", "--no-checkout", "--no-track", "-b", branch, path)
    registry.add(path, branch)
    schedule(lambda: git(path, "checkout"))   # deferred
    return path
```

## 5. Constants & knobs

| Name | Value / form | Notes |
|---|---|---|
| Worktree suffix | 6 hex chars | random per worktree |
| Default branch prefix | `claude` | user-configurable |
| Shim socket mode | 0600 | per session |
| Shim protocol version | 1 | `{v,key,url}` |
| Dev launch file | `.claude/launch.json` | servers receive `PORT` |
| Managed-config keys | ~183 | MDM / GPO / JSON file |
| VM rootfs | ~1.2 GB | downloaded |
| `CLAUDE_CODE_LOCAL_BINARY` | path | dev override for the engine |

## 6. Rationale

- Running the real engine and the real web app lets the product UI and the agent engine ship on independent schedules, and keeps behaviour identical across surfaces.
- Pinning by version and hash makes it trivial to tell which engine produced a session, and makes rollbacks reliable.
- Checking origin and frame defends against a compromised sub-frame that could otherwise call privileged channels by name.
- Utility processes isolate hangs and crashes from the UI thread.
- Per-tab storage partitions and session-scoped loopback ports stop previews from leaking cookies or reaching other local services.

## 7. Harness design takeaways

- **T1 Thin host, real engine.** Keep the GUI a thin shell around the unmodified engine and the existing web front-end, so each ships on its own cadence.
- **T2 Pin and verify the engine.** Store the engine binary under a version-and-hash path, mark it verified, and allow a dev-only env override. Rollback and provenance become simple.
- **T3 Validate IPC by origin and frame.** Typed, generated channels plus a top-frame and origin-allowlist check on every handler keep untrusted frames away from privileged APIs.
- **T4 Isolate heavy or fragile work.** Put PTYs, git, indexing, search, MCP hosting and stall sampling in watched utility processes, so no single hang freezes the UI.
- **T5 Partition previews and scope localhost.** Give each preview its own storage partition. Route `$BROWSER` through a socket bridge that admits only loopback ports the session owns.
- **T6 Deny-by-default sidecars.** Sandbox helpers with no network and no home access, and prevent child processes from inheriting the app's OS privacy grants.
- **T7 Lock the runtime.** Disable Node escape hatches with fuses, enforce archive integrity, and ship an enterprise policy layer with a documented key set that does not depend on trusting the user's environment.

## 8. Uncertainties

- The main-window renderer was not examined.
- Compiled bytecode caches and native add-ons were not decoded.
- The browser native-messaging protocol and the scheduled-task loop were not located.
- The fuse bit ordering is assumed to follow the standard layout.
