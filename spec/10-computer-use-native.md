# 10 — Computer use: native layer (functional specification, macOS arm64)

> Clean-room functional specification. Derived from analysis notes; contains no original source or prompt text.

---

## 1. Purpose & scope

Specifies the native components beneath computer use: screen capture, input injection, accessibility (AX) actions, OS-permission (TCC) handling, user-coexistence detection, on-screen indicators, and the out-of-process helper for private window-server functionality. Policy, tool surface and the JS dispatch pipeline are in [09-computer-use.md](09-computer-use.md).

---

## 2. Concepts & data model

### 2.1 Components

| Component | Kind | Host | Responsibility |
|---|---|---|---|
| Capture/AX add-on | in-process native module (Swift) | CLI and desktop | screen & window capture, display info, app/window management, AX actions, TCC checks, Esc hotkey (CLI) |
| Input add-on | in-process native module (Rust, wrapping an open-source cross-platform input library) | CLI; desktop equivalent in a general native binding | global keyboard/mouse events |
| General native binding (desktop) | in-process native module (Rust) | desktop | input incl. paced typing, JPEG frame encoding, save-panel AX probes, session-state reads, misc OS shims |
| Raw-event helper | separate executable (Rust), JSON-RPC over stdio | desktop | per-process event posting and focus tricks via private window-server API |
| Spawn-disclaim wrapper | tiny executable (C) | desktop | spawn children without lending the app's TCC responsibility |
| Voice capture add-on | in-process | CLI | dictation only — not part of computer use |

```
CLI (terminal is TCC-responsible)                Desktop (app bundle is TCC-responsible)
┌──────────────────────────────────┐            ┌───────────────────────────────────────────┐
│ executor (JS)                    │            │ executor (JS, main process)               │
│  ├─ capture/AX add-on ◄─ 1 ms    │            │  ├─ capture/AX add-on (full + app scope)  │
│  │   run-loop pump during calls  │            │  ├─ native binding: global input, JPEG    │
│  ├─ input add-on (global events) │            │  ├─ helper ══JSON-RPC/stdio══► raw events │
│  └─ clipboard via OS CLI tools   │            │  │      to a PID, key-window illusion,    │
└──────────────────────────────────┘            │  │      space moves, compositor wake      │
                                                │  └─ app-level Esc shortcut (kill switch)  │
                                                └───────────────────────────────────────────┘
```

### 2.2 Capture/AX add-on interface (shape the implementer should provide)

All methods return promises; arguments validated with explicit messages.

| Namespace | Methods |
|---|---|
| screenshot | `captureExcluding(allowedBundleIds, quality, outW, outH, displayId?)` → `{base64, width, height, displayWidth, displayHeight, displayId, originX, originY, captureError?}`; `captureRegion(allowed, x, y, w, h, outW, outH, quality, displayId?)` |
| display | `getSize(displayId?)` → `{width, height, scaleFactor, originX, originY, isPrimary, label}`; `listAll()`; `windowNumberAtPoint(x, y)` |
| apps | `prepareDisplay(allowed, hostBundleId, displayId?)` → `{hidden[], activated}`; `previewHideSet(exempt)`; `unhide(ids)`; `findWindowDisplays(ids)`; `appUnderPoint(x, y)`; `listInstalled()` (+ running variant; via Spotlight metadata query for application bundles, with an "index incomplete" flag); `listRunning()`; `open(bundleId, activates)` → first window id; `iconDataUrl(path)` (PNG); `resolveBundleIds(names)` |
| combined | `resolvePrepareCapture(allowed, host, quality, w, h, displayId, autoResolve, doHide)` — pick display, hide, capture in one round trip |
| tcc | `checkAccessibility`, `requestAccessibility`, `checkScreenRecording`, `requestScreenRecording` |
| hotkey (CLI) | `registerEscape(cb)`, `unregister()`, `notifyExpectedEscape()` |
| run loop | `drainMainRunLoop()` |
| appScoped | `listWindows`, `captureWindow(bundle, windowId?, w?, h?, x?, y?, listElements?)`, `zoomWindow(…, scale∈(0,1])`, `probe(bundle, windowId?, x, y, intentJson, dryRun, target?)`, `menu(bundle, {mode, path})`, `windowOwner`, `windowDocument`, `isWindowAxLive`, `webPageHasContent`, `waitUntilWebTreeIsReady`, `setManualAccessibility(bundle, on)`, `toggleSubstitutions(bundle, on)`, `activatePid`, `frontmostPid`, `armFocusStealGuard(targetPid?, restoreToPid, deadlineMs)`, `showWindowBehindFront(pid, windowId)`, `setPhantomCursor(x, y, windowId?, pulse?)`, `hidePhantomCursor`, `collisionWatch/unwatch(bundle)`, `setCollisionCallback`, `sessionGuardState()` → `{secureInputPid, screenLocked}`, background-menu listener and per-action begin/returned tokens |

### 2.3 Probe result (app scope)

```
ProbeResult {
  dispatch: { outcome: "ok"|"ineffective"|"unsupported", reasonCode, axOp, role, subrole,
              predicted, axError, foreignPid, descentPath, textWritable,
              previousContent, previousContentTruncated, wouldActOn, lookupMismatch },
  element, screenPt, windowBounds, resolvedWindowId,
  axSummary, elementListId, summaryTruncated, foreignPanelPresent
}
```

### 2.4 Refusal reason vocabulary (closed enum; names are implementer's)

Secure field; foreign process owns element; window on another Space; window minimised; app hidden; sheet-dimmed area; popup menu open; context menu open; listed element covered; user active in app; focus did not follow; AX write was a silent no-op; toolkit ignores background drag; compositor transform active; recent hardware keydown would misdirect; secure input started mid-string; … Each is tagged either *raw-fallback-allowed* or *block*.

### 2.5 Helper protocol

Newline-delimited JSON-RPC 2.0 on stdin/stdout; stderr lines forwarded as debug logs.

| Method | Params (fields) | Purpose |
|---|---|---|
| `probe` | — | capability report: window-server API present & verified, auth envelope available, focus-without-raise, AX window lookup, AX trusted |
| `dispatchRaw` | `pid, windowId, kind, winLocalPt, button, count, modifiers, toWinLocalPt, viaWinLocalPts, dx, dy, ticks, keyName, text, typeBudgetMs, focusedTarget, partOfTextWrite, mouseVariant, hostPid, dryRun` | synthesise mouse/key/text events and post to the target process |
| `keyWindowSpoof` | `phase: begin|check|repair|end`, `pressed`, … | make a window behave as key without raising |
| `bringWindowToActiveSpace` | `windowId, dryRun, …` | move a window across Spaces |
| `wakeChromiumCompositor` | `pid, windowId, …` | force an occluded Chromium/Electron window to paint |

Errors: `{id, error: {message, data: {code}}}` with a closed code set (API unavailable, permission denied, invalid params, foreign pid, …).

---

## 3. Behavioural requirements

### 3.1 Capture

- **R1.1** Use ScreenCaptureKit (requires macOS 14): get shareable content → build a content filter of *display minus excluded applications, except selected windows* → configure width/height, **cursor hidden**, no audio, (desktop) capture resolution → take a single screenshot. Gate on the screen-capture preflight check; throw a permission error otherwise. Apply a watchdog for missing callbacks. Confidence: observed.
- **R1.2** Exclusion MUST remove non-granted apps and the agent's own host (terminal in CLI mode is treated as a surrogate host: exempt from hiding, not activated, excluded from capture) at compositor level — no post-hoc redaction. Confidence: observed/inferred.
- **R1.3** Per-window capture (app scope) MUST use a desktop-independent-window filter including child windows, so occluded/background windows can be captured. Errors: window not found, region outside window, foreign panel present.
- **R1.4** Output size computed by JS (see 09 §4.1); native draws into a bitmap context at that size and encodes JPEG with compression factor 0.75, returned as a base64 data URL. Icons are PNG. Desktop also has a raw-frame JPEG encoder for live preview.
- **R1.5** Every capture takes an optional display id and returns display origin so coordinates map back to global space.
- **R1.6** `prepareDisplay` hides non-allowed apps (standard hide) and returns the hidden list for later unhide. A fixed set of system UI processes (wallpaper, control centre, menu-bar server, input switcher, Wi-Fi agent, accessibility UI, login window, file manager, notification centre, system screenshot UI, popular screenshot tools) MUST never be hidden. Confidence: observed (list) / inferred (purpose).
- **R1.7** Legacy window-list APIs remain for enumeration and fallback only.

### 3.2 Global input (display scope)

- **R2.1** Post events at the HID level via CGEvent. Typing: send Unicode strings in chunks ("fast text"), falling back to per-character events. Keys map to virtual keycodes for the **current keyboard layout** via the Text Input Sources + key-translation APIs, falling back to an ASCII-capable layout; unmapped characters temporarily borrow an unused keycode.
- **R2.2** Key vocabulary ≈100 named keys incl. modifiers, F1–F20, media keys (sent as system-defined events). Buttons: left, right, middle, scroll up/down/left/right. Actions: press, release, click. Axes: horizontal, vertical.
- **R2.3** Desktop adds paced typing and key-name validation.
- **R2.4** Clipboard paste path: save → write → verify round-trip → paste chord → restore.

### 3.3 App-scope input (background)

- **R3.1** AX first: hit-test the element at the point (element-at-position, record descent path, resolve its window) and attempt a semantic action: press, show menu, set selected, set focused, set selected text range (caret from range-for-position / bounds-for-range), insert via selected text, whole-field replace via value (only if overwrite permitted), set scrollbar value, or menu-bar traversal.
- **R3.2** Only when AX returns *unsupported* with a reason tagged raw-fallback-allowed, call the helper's `dispatchRaw`; it posts events **to the target process** with an authentication envelope so the app accepts non-HID events. Unknown reasons MUST block (fail closed).
- **R3.3** Text in 512-char slices with a time budget and 4000-char cap; partial delivery reports the delivered prefix length.
- **R3.4 Focus management**: key-window illusion without raising (begin/check/repair/end); focus-steal guard with leases that restores the user's frontmost app if a launched app steals focus; place a window behind the front window; move windows across Spaces; wake occluded Chromium compositors (detect Electron/Chromium framework presence).
- **R3.5 Web content AX**: enable manual/enhanced accessibility on Chromium/Electron apps, then poll until the web AX tree is ready before acting.
- **R3.6 Smart substitutions**: temporarily disable the target app's smart quotes/dashes/text replacement via its preferences so typed text arrives verbatim. Confidence: inferred.
- **R3.7 App launch** via workspace open with an activates flag; return first window id; error if no app for bundle id.

### 3.4 OS permissions (TCC)

- **R4.1** Accessibility: trusted-process check (with prompt option for request). Screen Recording: preflight / request screen capture access. Microphone (voice only): media authorisation.
- **R4.2** Desktop exposes an IPC surface: get state, request accessibility, request screen recording, request microphone, open a System Settings pane, revoke a session grant for a bundle, list installed apps. Booleans map to granted / not-determined.
- **R4.3 Responsible process**: in CLI mode the terminal app holds the grants; in desktop mode the app bundle does, and the in-process add-ons and the helper inherit it (helper is spawned *without* disclaiming). Confidence: inferred.
- **R4.4** When spawning untrusted or non-system-owned binaries, the desktop MUST use the disclaim wrapper (POSIX spawn with responsibility disclaimed; options for process group, resetting exception ports, explicit responsible) so user tools can't ride on the app's grants.
- **R4.5** Refuse actions when the screen is locked or another process holds Secure Input (from the current session dictionary).

### 3.5 Indicators, interrupts, coexistence

- **R5.1 Phantom cursor**: non-activating, mouse-transparent panel at a high window level with an animated shape layer pulse, showing where the agent acts in a background window without moving the real cursor.
- **R5.2 Window cover** (purpose inferred): snapshot panel tied to a window id, likely to mask flicker during background manipulation.
- **R5.3 Session halo**: coloured halo around another app's window; hue = string hash of session id mod 360; label = first 8 chars of session id; exposed as `halo_attach`/`halo_detach`.
- **R5.4 Esc**: CLI installs a keyboard event tap on the main run loop (requires Accessibility); `notifyExpectedEscape` suppresses the agent's own Esc. Desktop registers an app-level global Escape shortcut while a lock is held, with a counter absorbing synthetic presses; handler stops the lock holder and all app-scope holders.
- **R5.5 Collision watch**: observe app-activation notifications; report when the user activates a driven app.
- **R5.6 User-activity gating**: seconds since last input event → "user active in app"; helper reports "user actively typing" (JS retries with backoff); recent hardware keydown blocks to avoid misdirected keys.
- **R5.7 Background menu guard**: an AX observer watches for menus the agent opened in background apps and closes them (tracking which were closed vs. left to the user).

### 3.6 Threading and timeouts

- **R6.1** Under Node/Bun the JS thread does not run the main CFRunLoop; while any native call is in flight the host MUST pump the run loop every 1 ms. Async native work bridges back via thread-safe functions.
- **R6.2** Every native promise wrapped in a 30 s timeout; screenshots have a 5 s backstop.

### 3.7 Helper supervision

- **R7.1** Per-request timeout 8 s base, +50 ms per typed char (min 1 s for typing), +400 ms per drag via-point; deadlines accumulate across queued requests.
- **R7.2** Restart backoff 0 s, 1 s, 5 s, then 60 s cooldown. Crash-loop breaker: after repeated crashes with no successful round trip, pause respawns 300 s. SIGKILL after consecutive timeouts.
- **R7.3** `cancelAll`: close stdin cooperatively with 700 ms grace; SIGKILL immediately if more than one request is pending.
- **R7.4** Missing binary → "API unavailable"; callers degrade to an "unavailable" path. Errors MUST say whether the request reached the helper (i.e., whether a side effect may have occurred).
- **R7.5** Helper on start: set a background activation policy (no Dock icon — inferred), log readiness with its pid to stderr, write rotating logs in the app's log folder; resolve private API symbols at runtime and report missing ones; self-check AX trust.

---

## 4. Algorithms

### 4.1 AX-first with raw fallback

```python
UNSUPPORTED_CACHE = {}   # (window, intent_kind) -> (hits, until)

def app_act(target, intent):
    key = (target.window, intent.kind)
    hits, until = UNSUPPORTED_CACHE.get(key, (0, 0))
    if hits >= 3 and now() < until:
        r = Probe(outcome="unsupported", reason=last_reason[key])
    else:
        r = ax.probe(target.bundle, target.window, *intent.point, json(intent), dry_run=False)
    if r.outcome == "ok": return r
    if r.outcome == "unsupported":
        UNSUPPORTED_CACHE[key] = (hits + 1, now() + 5)
        if REASONS.get(r.reasonCode) == "raw_ok":
            if ax.windowOwner(target.window) != target.grant.bundleId: raise Blocked("owner_mismatch")
            if session_guard().secureInputPid or session_guard().screenLocked: raise Blocked("guard")
            return helper.call("dispatchRaw", build_raw(intent, target), retry_on="user_typing",
                               retries=6, delay=0.4)
        raise Blocked(r.reasonCode)          # includes unknown codes: fail closed
    raise Ineffective(r)                     # report; model decides
```

### 4.2 Typing strategies

```python
def type_text(text, mode):
    if mode == "app_scope":
        delivered = 0
        for chunk in surrogate_safe_chunks(text[:4000], 512):
            r = app_act(target, Intent("text", chunk), budget=remaining(120))
            delivered += r.delivered
            if r.partial: return Partial(delivered)   # tell model not to retype everything
        return Ok()
    if long_or_multiline(text) and clipboard_allowed:
        return paste_with_restore(text)               # verify round trip
    for piece in text:                                 # display scope
        if is_shortcut_or_control(piece): post_keycode(layout_keycode(piece))
        else: post_unicode_string(piece)
```

### 4.3 Helper supervisor

```python
class Helper:
    BACKOFF = [0, 1, 5]; COOLDOWN = 60; BREAKER_PAUSE = 300

    def call(self, method, params):
        self.ensure_running()
        budget = 8.0
        if method == "dispatchRaw" and params.get("text"):
            budget += max(1.0, 0.05 * len(params["text"]))
        budget += 0.4 * len(params.get("viaWinLocalPts", []))
        deadline = max(self.queue_tail_deadline, now()) + budget
        self.queue_tail_deadline = deadline
        try:
            return self.rpc(method, params, deadline)
        except Timeout:
            self.consecutive_timeouts += 1
            if self.consecutive_timeouts >= LIMIT: self.proc.kill(SIGKILL)
            raise HelperError(reached=True)
        except NotSent:
            raise HelperError(reached=False)

    def on_exit(self):
        self.crashes += 1
        if self.crashes_without_roundtrip >= N: self.paused_until = now() + self.BREAKER_PAUSE
        else: self.next_spawn = now() + (self.BACKOFF[self.crashes-1]
                                         if self.crashes <= 3 else self.COOLDOWN)

    def cancel_all(self):
        if self.pending > 1: self.proc.kill(SIGKILL)
        else: self.proc.stdin.close(); wait(0.7) or self.proc.kill(SIGKILL)
```

### 4.4 Combined prepare-and-capture

```python
def resolve_prepare_capture(allowed, host, quality, w, h, preferred, auto, do_hide):
    display = preferred
    if auto: display = most_common(find_window_displays(allowed)) or preferred or primary()
    hidden = prepare_display(allowed, host, display) if do_hide else []
    img = capture_excluding(allowed_minus(host), quality, w, h, display)
    return img | {"hidden": hidden, "displayId": display}
```

---

## 5. Constants & knobs

| Name | Value |
|---|---|
| JPEG compression | 0.75 |
| Image fit | ≤1568 px edge, ≤1568 tokens at 28 px/token (computed in JS) |
| Minimum OS for capture / app scope | macOS 14 / macOS 15 |
| Run-loop pump interval | 1 ms |
| Native call timeout / screenshot backstop | 30 s / 5 s |
| Mouse animation | ease-out cubic, 60 fps, ≤0.5 s; drag press delay 50 ms |
| App-scope text | 512-char slices, 4000-char cap |
| Helper timeout | 8 s + 50 ms/char (≥1 s) + 400 ms/via-point |
| Helper restart backoff | 0, 1, 5 s then 60 s |
| Crash-loop pause | 300 s |
| Cancel grace | 700 ms |
| User-typing retry | 6 × 400 ms |
| Unsupported cache | 3 hits → 5 s |
| Halo hue / label | hash mod 360 / first 8 chars |

---

## 6. Rationale

- **Blast-radius split**: public, stable APIs are safe in-process; private window-server calls change between OS releases and can segfault or hang — a crash in the GUI main process would kill the app, whereas a child can be killed and restarted. The child shares the app's TCC identity, so isolation costs no permissions, and it owns its own run loop so event posting doesn't contend with the GUI loop.
- **Compositor-level exclusion** is cheaper and safer than pixel redaction; hiding the cursor keeps frames stable.
- **Per-PID events** let the agent drive a background window without stealing the user's cursor or focus; semantic AX actions are more reliable than pixel clicks.
- **Three typing paths** cover speed (Unicode strings), shortcuts on non-US layouts (keycodes), and long text (clipboard).
- **Disclaiming TCC responsibility** prevents arbitrary user tools from inheriting the app's sensitive grants.

---

## 7. Harness design takeaways

1. **T1 — Split by blast radius, not language.** Public APIs in-process; private or crash-prone APIs in a supervised child with a capability probe, crash-loop breaker and per-method timeouts.
2. **T2 — One native call for display choice, hiding and capture**, using a compositor filter that excludes non-granted apps and your own UI; cursor off; JPEG ≈0.75.
3. **T3 — Size images in one pure function** mapping (logical × scale) to the largest size within edge and token caps; return origin and size so coordinates map back exactly.
4. **T4 — Semantic AX first; raw events only on a classified refusal**; unknown reasons fail closed; raw events go to a process, not the HID stream.
5. **T5 — Budget for coexistence with the user**: typing/HID detection, Secure Input, screen lock, lease-based focus restore, Esc kill switch ignoring own presses, "user took the app" callback.
6. **T6 — Show where the agent acts without moving the user's cursor**: click-through high-level overlays (phantom cursor, session-coloured halo) excluded from captures.
7. **T7 — Three typing strategies** (Unicode events, layout-aware keycodes, clipboard paste with verify/restore); disable smart substitutions; slice long text and report partial delivery.
8. **T8 — Respect run loop and TCC identity**: pump CFRunLoop during calls under Node/Bun, time out every native promise, know the responsible process, disclaim for untrusted children.
9. **T9 — Chromium apps need care**: enable manual accessibility, wait for the web AX tree, wake occluded compositors.
10. **T10 — Closed telemetry vocabularies**: clamp roles, enumerate reason codes and method names so app content never leaks into analytics.

---

## 8. Uncertainties

- Exact excluded-apps / excepted-windows sets in the capture filter not fully traced (all non-allowlisted running apps vs. a narrower set). Confidence: inferred.
- Byte layout of the key-window illusion event record and the authentication-envelope version were not decompiled.
- Window cover has no caller in the analysed build; purpose inferred.
- The alternative native mouse variant and an "offscreen primer" mouse sequence are unexplained.
- Watch-record and teach-overlay native paths were not analysed.
- An attested IPC channel in the general native binding (possibly to a privileged helper) was not traced.
