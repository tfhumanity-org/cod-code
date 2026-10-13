# 09 — Computer use: tool surface, grants, safety and execution pipeline (functional specification)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

Specifies the **policy and orchestration layer** of a "computer use" (CU) capability that lets a coding agent see and operate desktop applications: the MCP tool surface, the grant model, safety gates, coordinate handling, locking/interrupts, cleanup, and the dispatch pipeline down to the native executor. The native layer (capture, input injection, accessibility, OS permissions, helper process) is specified in [10-computer-use-native.md](10-computer-use-native.md).

Applies to two hosts sharing one core: a terminal CLI and a desktop GUI app. Each host supplies an **executor** (platform adapter) and **session hooks** (grants, locks, dialogs, abort).

---

## 2. Concepts & data model

### 2.1 Layering

```
  model tool call (mcp__computer-use__<action>)
            │
  ┌─────────▼──────────────── host wrapper ─────────────────────┐
  │ machine lock · Esc kill switch · approval cards · takeover  │
  └─────────┬───────────────────────────────────────────────────┘
            │ dispatch(name, args)
  ┌─────────▼──────────────── shared core ──────────────────────┐
  │ preflight: enabled → OS perms → allowlist → lock            │
  │ handler: frontmost gate · tier check · hide others ·        │
  │          hit-test · delivery re-check · coordinate scaling  │
  └─────────┬─────────────────────────────┬─────────────────────┘
            │ display-scope executor      │ app-scope executor (desktop, macOS ≥15)
     in-process capture/AX add-on         AX-first semantic actions
     in-process global input add-on       + supervised raw-event helper (see 10)
```

The server is registered as an MCP server named `computer-use` (server version string observed: 0.2.0). Confidence: observed.

### 2.2 Control modes

- **Display scope** (full screen): screenshot + click/type with the global cursor and keyboard. Takes over the machine, shows a glow border, holds a global lock.
- **App scope** (background, desktop on macOS 15+): drives one window via Accessibility, with per-process raw events as fallback; the window is never raised. Default preferred mode on desktop is background.

### 2.3 Session grant record

```
Grant  { bundleId, displayName, grantedAt, tier: "read"|"click"|"full" }
Session {
  grants: [Grant]            # monotonic within a session
  clipboardRead, clipboardWrite, systemKeyCombos: bool
  takeoverApproved: bool     # desktop: separate consent
  hiddenDuringTurn: [bundleId]
  clipboardStash?: bytes
  lastScreenshot?: Frame
  coordinateMode: "pixels" | "normalized_0_100"   # frozen at session start
}
Frame { width, height, displayId, displayWidth, displayHeight, originX, originY, scaleFactor }
```
Remote/dispatched grants on desktop expire after 30 min. Confidence: observed.

### 2.4 Tool surface (public tool names)

| Tool | Inputs | Notes |
|---|---|---|
| `request_access` | `apps[]`, `reason`, `clipboardRead?`, `clipboardWrite?`, `systemKeyCombos?` | must be first; one dialog for all; repeat calls add apps; does not grant screen takeover |
| `screenshot` | `scale?` (adaptive resolution only), `save_to_disk?` | description varies by filtering mode |
| `zoom` | `region[x0,y0,x1,y1]`, `scale?` | region in last full screenshot's frame; read-only; clicks never refer to zoom image |
| `left_click`, `double_click`, `triple_click`, `right_click`, `middle_click` | `coordinate[2]`, `text?` (held modifiers) | |
| `type` | `text` | |
| `key` | `text` chord, `repeat ≤100` | system combos need grant |
| `scroll` | `coordinate`, `scroll_direction`, `scroll_amount ≤100` | |
| `left_click_drag` | `coordinate`, `start_coordinate?` | |
| `mouse_move`, `left_mouse_down`, `left_mouse_up`, `cursor_position`, `hold_key{text,duration}`, `wait{duration ≤100}` | | |
| `open_application{app}`, `switch_display{display\|"auto"}`, `list_granted_applications`, `read_clipboard`, `write_clipboard` | | |
| `list_apps{query, running_first, limit, cursor}` | | app-scope hosts only |
| `computer_batch` | `actions[]` (same action enum), `save_to_disk?` | gate per action; coordinates refer to pre-batch screenshot |
| Teach: `request_teach_access`, `teach_step{explanation, next_preview, anchor?, actions[]}`, `teach_batch{steps[]}` | | gated |
| App scope: `request_full_control`, `release_full_control`, `app_list_windows`, `app_ax_find{role, title_contains}`, `app_screenshot{app, window_id, scale}`, `app_click{coordinate\|element_index\|target:"focused", button, count}`, `app_type{text, mode: insert\|replace, overwrite_existing, disable_substitutions}`, `app_key{combo}`, `app_scroll{dy}`, `app_drag`, `app_menu{path\|list}`, `app_batch`, `app_release`, `app_bring_to_current_space` | | drag and bring-to-space only when raw input is supported |

Every input tool's description ends by noting the frontmost app must be granted.

**Desktop remote surface**: the same tools re-exported with a `computer_` prefix under a remote-devices server, plus `computer_resolve_access`, `computer_release_lock`, `computer_app_zoom`, `computer_app_focus`. A cloud-side enabling stub only tells the model to use existing tools and not to claim the computer is connected before a call succeeds.

**Sibling servers (desktop)**:
- *Framebuffer* (`framebuffer_list/attach/screenshot/zoom/cursor_position/click/move/drag/scroll/type/key/hold_key`) for VMs and launch-config sources of type framebuffer; input requires a gate and per-action approval; **refuses a click if pixels at the target changed since the last screenshot**; zoom crops the last frame.
- *Window halo* (`halo_attach{pid|bundlePath|appName, label, hue, cornerRadius}`, `halo_detach`): coloured glow behind a target window, hue derived deterministically from a hash of the session id. Present but disabled.

### 2.5 Installed-app list embedded in tool description

- Sources: a fixed always-included set of common apps plus apps in the system and user Applications folders.
- Drop helpers, agents, updaters, dot-names; drop names >40 chars or outside a safe charset.
- Cap 50 entries plus an "and N more" line; 1 s enumeration budget.
- Wrapped in a tag declaring the content is data only.

### 2.6 Tier categories

| Category | Examples | Tier | Allowed |
|---|---|---|---|
| browser | ~36 macOS browsers (Safari, Chrome family, Edge, Firefox, Arc, …) | read | screenshots only; redirect to browser-automation tools |
| trading / crypto | brokerage and wallet apps + ~60 name keywords | read | screenshots only |
| terminal / IDE / automation | terminals, code editors, JetBrains, Xcode, script/automation tools | click | plain left-click and scroll only; no typing, keys, right/modifier click, drag; use the shell tool instead |
| Windows shell surfaces | explorer, search, start | click | |
| everything else | | full | |

Tier order `read(0) < click(1) < full(2)`. Capability kinds: `mouse_position` (always), `mouse` (≥click), `mouse_full`, `keyboard` (full).

---

## 3. Behavioural requirements

### 3.1 Registration & gating

- **R1.1** The CU server MUST enforce all policy itself; the host MUST pre-allow all CU tool names in the generic permission system so the user isn't asked per click. Confidence: observed.
- **R1.2** CLI host: register as a dynamic stdio server but run it in-process via a linked transport pair, dispatching with the live tool context. Desktop: add the server to allowed tools and route `request_access` through the host's own approval card as a pseudo-tool.
- **R1.3** Availability gated by plan tier, a remote config, a per-user setting, and MUST be disabled under regulated-data (e.g. HIPAA) accounts. App scope requires macOS ≥15 (and below a tested ceiling).
- **R1.4** Sub-options with defaults: pixel validation off, multi-line clipboard paste on, mouse animation on, hide-before-action on, auto-target display on, clipboard guard on, mask fail-closed on, adaptive resolution off (CLI) / on (desktop), coordinate mode pixels — frozen per session.

### 3.2 Grants

- **R2.1** Names MUST be resolved case-insensitively against installed and running apps (prefer system app folder; special-case the file manager). Unknown names return edit-distance suggestions. If **any** name fails, the whole request fails with no dialog; if the app index was incomplete, suggest the explicit app-mention path.
- **R2.2** Requests for the agent's own host app MUST be refused permanently (would let the model change its own permissions).
- **R2.3** Auto-deny: user deny list (settings), a fixed policy deny list of media/DRM apps and some content sites by name substring, and any bundle id beginning with `<`.
- **R2.4** A wildcard "all apps" grant MUST only be constructible with a non-empty policy deny list (construction otherwise is a programming error).
- **R2.5** A requested tier MAY only be lowered to the category tier, never raised.
- **R2.6 First-request friction**: the first request for a browser or terminal returns an explanatory warning instead of a dialog, telling the model to retry in the same turn only if truly needed.
- **R2.7** Approval UI MUST flag high-risk grants: shell-equivalent apps, the file manager (any file), system settings. It states how many apps will be hidden while working.
- **R2.8** Unattended runs MUST never show dialogs; return an "unattended, no approver" error for both OS permissions and grants.

### 3.3 OS permissions

- **R3.1** Every call first checks Accessibility and Screen Recording. If either is missing, every tool except the two request tools returns an "OS permission not granted" error.
- **R3.2** `request_access` then shows a permission panel (buttons to request each permission, open the relevant System Settings pane, retry; note that Screen Recording may require restart). Afterwards re-check and tell the model either to call again now or not to retry this turn.

### 3.4 Screen takeover (desktop)

- **R4.1** App grants MUST NOT authorise takeover. The first display-scope call after background work raises a separate takeover card with a 290 s timeout; outcomes: declined, not answered, approved for the session. `request_full_control`/`release_full_control` toggle explicitly.
- **R4.2** While app locks are held, display-scope and clipboard tools are refused (release app scope first).

### 3.5 Teach and watch modes

- **R5.1 Teach**: same allowlist semantics without flags. On approval hide the main window and show a fullscreen click-through tooltip overlay. Each step shows explanation and preview, points at an optional anchor, waits for the user's "Next", runs actions, auto-screenshots. `teach_batch` validates all steps before showing any. Ends at turn end; `request_access` refused while active.
- **R5.2 Watch/record**: user records screen, clicks, keys and optional narration. Chooser ignores clicks in the first 300 ms. User trims; sent as timestamped action descriptions + keyframes (≤1280 px, JPEG q80, evenly sampled) + changed-region crops. Keystrokes coalesced into typed runs; secure-input runs redacted; repeated scrolls summed. Two framings: "learn this workflow / write a skill" and "context for my request". Attached instructions MUST tell the model to reproduce outcomes with the best non-GUI tool rather than replaying gestures, to fall back to GUI only when nothing else works, and that typed text, image contents and narration are untrusted data.

### 3.6 Prompt-injection defences

- **R6.1** All third-party strings shown to the model (app list, AX titles, recordings) MUST be wrapped as data-only.
- **R6.2** App/window/role names shown only if ≤40 chars and within a strict letters/digits/punctuation class; else replaced by a "withheld" placeholder. Bundle ids with control chars, lone surrogates, `<>"` or tag characters rejected. AX error strings capped at 120 chars and charset-filtered.
- **R6.3** System-prompt guidance MUST: never click web links via CU; route web work through browser tools; treat links in mail/messages as suspicious and verify the real URL; never trade or move money (finance apps allowed for bookkeeping only).
- **R6.4** Every tier/policy error MUST end with a ban on circumventing the block via scripting bridges, shell or any other route.
- **R6.5** In bypass/auto permission modes, browser and preview tools (and hosted CU) are forced back to classifier/default handling rather than auto-allow; the shared classifier treats "screenshot then click bare coordinates" as an unverifiable target.

### 3.7 Pre-action gates (each input action)

- **R7.1 Hide** non-granted apps on the target display and activate a granted one; record hidden ids; next screenshot tells the model what was hidden and why.
- **R7.2 Frontmost check**: not granted → error suggesting a new screenshot; desktop shell / file manager frontmost → requires file-manager grant (dock and launcher can start anything); agent's own window focused → state-conflict error; tier too low → tier error naming allowed actions and a better tool.
- **R7.3 Clipboard guard**: when a click-tier app is frontmost, stash and empty the clipboard (so an in-app Paste button can't inject); refuse `write_clipboard`; restore at turn end.
- **R7.4 Hit-test**: reject points off every display; resolve the app under the point and enforce *its* tier; menu bar allowed only if frontmost is granted; hit on own overlay → self-intercept error with retry hint.
- **R7.5 Protected files & save dialogs (macOS)**: refuse typing into windows whose document is a shell rc file, VCS config, SSH key, or login-agent/daemon plist (navigation keys still allowed). With a Save sheet open: single navigation keys only; file names must not contain `/`, `..`, `~` or protected names; no tab/newline; no Return in go-to-folder; no saving into protected folders; Cancel always allowed. Windows file dialogs: no address bar, search, right-click, alt-click, drag-drop.
- **R7.6 Delivery re-check**: right before keystrokes and between graphemes / key repeats, compare the frontmost app with the one approved at gate time; on change stop and report exactly what was already typed.
- **R7.7 Pixel staleness (optional)**: compare a 9 px patch at the target between last screenshot and fresh capture; warn on change.
- **R7.8 System key combos** (quit, app switch, launcher, space switching, close window, lock, …) need the system-combo grant; clipboard chords are classified separately.

### 3.8 Locks, interrupts, indicators

- **R8.1** One machine-wide holder. CLI: a lock file in the user config dir with `{sessionId, pid, acquiredAt}`, created exclusively; stale locks recovered by checking whether the PID is alive. Desktop: in-memory broker with one exclusive holder plus per-(app, window) holders, emitting change events.
- **R8.2 Esc kill switch** while a CU call is in flight aborts the turn. The agent's own Esc presses MUST be pre-announced (or counted) so they don't trigger it. Long loops (type, key repeat, wait, hold, batch) poll abort between steps. An OS notification announces control and how to stop.
- **R8.3 User collision (app scope)**: if the user activates the driven app, evict the lock, back off re-acquisition and tell the model to ask first; raw dispatch reports "user typing" and retries 6 × 400 ms; refuse typing during secure input.
- **R8.4 Emergency stop (desktop remote)**: for 15 min after a stop, every call except request/resolve/release returns a stopped error.
- **R8.5 Glow overlay**: transparent click-through top-level panel with pulsing inset glow and a badge saying the agent is in control moving from centre to corner; hidden from window overview; respects reduced motion; toggled on lock changes. App scope uses a phantom cursor instead.

### 3.9 Hiding own UI from captures

- **R9.1** macOS: native filtering excludes non-granted apps (agent included) at compositor level. Windows: mask with solid rectangles, fail closed. Linux: no filtering, and the description says so.
- **R9.2** GUI host captures set content protection on all its windows during capture.
- **R9.3** Desktop click-through: before each mouse action, make own windows within 16 px of the target ignore mouse events, poll the window-at-point until own window no longer wins, restore after the input burst.

### 3.10 Cleanup at turn end

- **R10.1** Un-hide hidden apps (5 s budget), restore clipboard, unregister Esc, release the turn lock, notify that control has ended. Locks also released on process exit; held mouse buttons released in `finally`, including after failed batches.

### 3.11 Dispatch

- **R11.1** Order: disabled → OS permissions → empty allowlist → acquire lock (on first non-request tool) → route (request/teach | app scope | display scope).
- **R11.2** Errors return `{isError, kind}` from a closed vocabulary (~30 kinds: insufficient tier, app not granted, lock held, state conflict, save panel restricted, …).
- **R11.3** Every native call wrapped with a 30 s timeout; screenshots additionally 5 s backstop; CLI pumps the main run loop every 1 ms during calls (see 10).

---

## 4. Algorithms

### 4.1 Screenshot sizing to token budget

```python
PX_PER_TOKEN, MAX_EDGE, MAX_TOKENS = 28, 1568, 1568

def tokens(w, h): return ceil(w / PX_PER_TOKEN) * ceil(h / PX_PER_TOKEN)

def fit(phys_w, phys_h):
    if max(phys_w, phys_h) <= MAX_EDGE and tokens(phys_w, phys_h) <= MAX_TOKENS:
        return phys_w, phys_h
    lo, hi = 1, max(phys_w, phys_h)          # binary search on long edge
    aspect = phys_w / phys_h
    while lo < hi:
        mid = (lo + hi + 1) // 2
        w, h = (mid, round(mid / aspect)) if aspect >= 1 else (round(mid * aspect), mid)
        if mid <= MAX_EDGE and tokens(w, h) <= MAX_TOKENS: lo = mid
        else: hi = mid - 1
    return dims_for(lo, aspect)

def screenshot(display, scale=1.0):
    w, h = fit(display.width * display.scaleFactor, display.height * display.scaleFactor)
    img = capture(display, w, h, jpeg_quality=0.75)
    if len(img) < max(64, 1024 * scale**2): img = capture(...)   # retry once
    if scale < 1: img = downscale(img, scale)   # frame stays w×h
    session.lastScreenshot = Frame(w, h, display...)
    return img, note(f"{scale}-scale view; coordinate frame {w}x{h}") if scale < 1 else img
```

### 4.2 Coordinate mapping

```python
def to_logical(x, y, sess):
    f = sess.lastScreenshot
    if sess.coordinateMode == "normalized_0_100":
        x, y = x / 100 * f.width, y / 100 * f.height
    if f is None:
        warn("no screenshot"); return x / display.scaleFactor, y / display.scaleFactor
    if not (0 <= x < f.width and 0 <= y < f.height):
        raise Teaching("coordinate outside the last screenshot's frame")
    lx = round(x * f.displayWidth / f.width) + f.originX
    ly = round(y * f.displayHeight / f.height) + f.originY
    return clamp_to_display(lx, ly, f.displayId)
```
Zoom maps its region to logical points and recaptures at fresh resolution (not a crop).

### 4.3 Action gate pipeline

```python
def run_input_action(sess, action):
    preflight(sess)                                # enabled, perms, allowlist, lock
    if opts.hideBeforeAction:
        sess.hiddenDuringTurn += prepare_display(sess.grants)
    front = frontmost_app()
    g = grant_for(front) or fail("app_not_granted", hint="take a new screenshot")
    if front.is_shell_or_file_manager and not has_file_manager_grant(sess): fail(...)
    if front.is_self: fail("state_conflict")
    require_tier(g.tier, kind_of(action))          # error names alternatives + workaround ban
    if g.tier == "click" and opts.clipboardGuard: stash_and_clear_clipboard(sess)
    if action.has_point:
        pt = to_logical(*action.coordinate, sess)
        if off_all_displays(pt): fail("hit_test_off_display")
        under = app_under_point(pt)
        if under.is_self_overlay: fail("self_intercept", hint="retry")
        if under.is_menu_bar: under = front
        require_tier(grant_for(under).tier if grant_for(under) else fail(...), kind_of(action))
    if action.is_keyboard: check_protected_documents_and_save_panels(front, action)
    if opts.pixelValidation and action.has_point and patch_changed(pt, 9): warn("screen changed")
    return deliver(action, approved_front=front)

def deliver_text(text, approved_front, sess):
    if "\n" in text and sess.clipboardWrite:
        return paste_via_clipboard(text)           # save, write, verify, cmd+v, wait 100ms, restore
    typed = ""
    for g in graphemes(text):
        if aborted() or frontmost_app() != approved_front:
            return partial(typed)                  # tell model exactly what was delivered
        key_for(g) if g in "\n\t" else type_unicode(g)
        typed += g; sleep(0.008)
```

### 4.4 Batch

```python
def batch(sess, actions):
    ref = sess.lastScreenshot                       # one coordinate reference
    hide_once(sess); results = []
    for i, a in enumerate(actions):
        try:
            results.append(run_input_action(sess, a, frame=ref,
                                            pixel_validation=False, auto_display=False))
        except Error as e:
            release_held_buttons()
            return results + [f"[{i+1}/{len(actions)}] {a.name}: FAILED {e}",
                              f"stopped at actions[{i}]"]
        sleep(0.010)
    return results                                   # images interleaved; last full screenshot becomes new reference
```

### 4.5 Mouse motion

- Move: post, then settle 50 ms (desktop: poll cursor location until it reads back target, ≤50 ms).
- Animated move (drags): ease-out cubic at 60 fps, duration `min(distance/2000, 0.5)` s, skipped if <0.03 s.
- Drag: press → 50 ms → animated move → release in `finally`.
- Modifier clicks: press modifiers, click, release in reverse order in `finally`.
- Scroll: move to point then wheel ticks per axis; app-scope scroll uses `dy × 40`.

### 4.6 App-scope dispatch ladder (summary; native details in 10)

```python
def app_dispatch(target, intent):
    r = ax_probe(target, intent)          # ok | ineffective | unsupported(reason)
    if r.ok: return r
    if r.unsupported and r.reason in RAW_FALLBACK_ALLOWED:
        verify_window_owner(target.window, target.grant.bundleId)
        return helper.dispatch_raw(pid=target.pid, window=target.window, ...)
    raise Blocked(r.reason)               # unknown reason → fail closed
```
Unsupported results cached per (window, intent) for 5 s after 3 hits. Text in 512-char surrogate-safe chunks, 4000-char cap, 120 s budget, partial delivery reported as delivered prefix length. `replace` mode selects all first; `overwrite_existing` refused by default and returns previous content (≤500 chars) for undo; `disable_substitutions` toggles autocorrect off and back. Menus via `app_menu` hold a key-window illusion; Services and clipboard menu items refused. If focus is stolen, restore the user's previous frontmost process after 500 ms and again after 3 s.

### 4.7 AX summary for `app_screenshot`

Return the JPEG plus ≤15 actionable elements (buttons, text fields, links, …) as `[N] role "title" (x,y)`, sanitised per R6.2, framed as data.

---

## 5. Constants & knobs

| Name | Value |
|---|---|
| Pixels per token / max edge / max tokens | 28 / 1568 / 1568 |
| JPEG quality | 0.75 |
| `scale` range | 0.1–1 (0.5 ≈ ¼ tokens) |
| Implausible frame retry | size < max(64, 1024·scale²) bytes, retry once |
| `repeat`, `scroll_amount`, `wait` max | 100 |
| Installed app list | 50 entries, 1 s budget, name ≤40 chars |
| Dispatch grant TTL (desktop remote) | 30 min |
| Takeover card timeout | 290 s |
| Teach chooser input protection | 300 ms |
| Watch keyframes | ≤1280 px, JPEG q80 |
| Pixel staleness patch | 9 px |
| User-typing retry | 6 × 400 ms |
| Emergency stop window | 15 min |
| Click-through radius | 16 px |
| Un-hide budget | 5 s |
| Native call timeout / screenshot backstop / run-loop pump | 30 s / 5 s / 1 ms |
| Mouse settle / animation fps / max duration / skip below | 50 ms / 60 / 0.5 s / 0.03 s |
| Drag press delay | 50 ms |
| Grapheme gap / paste settle | 8 ms / 100 ms |
| Windows paste threshold | >16 chars |
| Batch inter-action delay | 10 ms |
| AX summary elements | 15 |
| AX unsupported cache | 3 hits → 5 s |
| App text chunk / cap / budget | 512 chars / 4000 chars / 120 s |
| Previous-content return | ≤500 chars |
| Focus-restore checks | 500 ms, 3 s |
| AX error string cap | 120 chars |

---

## 6. Rationale

- A generic per-action approval prompt is useless at dozens of clicks per minute; policy belongs in the server where it can be specific.
- Three consents (apps, takeover, OS permission) match three distinct risks; one coarse prompt is either alarming or over-broad.
- Tiers cap capability by category so that a compromised or confused model can't type into a terminal or trade.
- The main real-world failure is the screen changing between screenshot and action; gating at delivery time closes that window.
- Screen text is attacker-controlled (window titles, web pages); sanitising and data-framing limit injection.
- The model is bad at arithmetic on display geometry; owning the coordinate system makes clicks exact and scale-independent.
- There is deliberately no automatic "wait for UI to settle" heuristic; the model is expected to `wait` or re-screenshot.

---

## 7. Harness design takeaways

1. **T1 — Policy in the tool server, tools pre-allowed.** Enforce grants, tiers, locks and OS permissions inside the server; skip generic per-call prompts.
2. **T2 — Separate consents** for which apps, screen takeover, and OS permission, each with its own card, error kind and timeout.
3. **T3 — Category tiers, lower-only.** Cap browsers, terminals and trading apps regardless of request; every tier error names the right alternative.
4. **T4 — Gate at delivery time.** Re-check frontmost app and hit-test target immediately before each input; report partial delivery precisely.
5. **T5 — Screen-derived strings are hostile.** Sanitise to a short safe charset, withhold the rest, wrap lists as data, ban workarounds in error text.
6. **T6 — Fail closed on unknown states** (unknown AX reasons, failed save-panel probe, wildcard grant without deny list).
7. **T7 — Own the coordinate system.** The model speaks screenshot pixels only; the server rescales, clamps and teaches on out-of-frame input; `scale` affects tokens, never coordinates.
8. **T8 — Budget screenshot tokens** (28 px/token, ≤1568 tokens and edge, JPEG ≈0.75), add `zoom` and `scale`, retry implausibly small frames once.
9. **T9 — Batch with pinned semantics**: one coordinate reference, per-action gate, stop on first error, interleaved images; same for teach steps.
10. **T10 — Kill switch and visible indicator**: global Esc ignoring own presses, glow, notifications, global lock with stale-PID recovery, timed emergency stop.
11. **T11 — Leave the desktop as found**: record and restore hidden apps and clipboard with a bounded timeout; release buttons/modifiers in `finally`.
12. **T12 — Supervise native helpers like services** (JSON-RPC over stdio, per-method timeouts, queued deadlines, restart backoff, crash-loop breaker, kill on repeated timeouts, "reached helper?" in errors).
13. **T13 — Steer away from pixels**: rank dedicated MCPs and CLIs above browser DOM tools above GUI clicking; turn demonstrations into outcome-level skills.

---

## 8. Uncertainties

- Whether pixel validation is ever live on desktop is unknown (patch-crop routine not located). Confidence: inferred.
- Server-side rollout values (app scope, batch-only, teach mode, mouse variant) unknown.
- Whether the auto-mode classifier sees CU inputs in the CLI is unclear.
- Windows/Linux executors (masking, UI-privilege isolation, portal restore tokens) only skimmed.
- Window halo is wired but disabled; intent unknown.
