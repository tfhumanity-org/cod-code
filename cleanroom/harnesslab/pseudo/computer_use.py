"""[PSEUDO] Computer use: policy server, coordinates, native seams + simulated backend (WP5).

LABEL: native screen capture, input injection, accessibility (AX) and the
supervised raw-event helper cannot sensibly be real stdlib Python. They are
expressed as ``Protocol`` interfaces (``ScreenCapture``, ``InputInjector``,
``AxDriver``, ``RawEventHelper``) with a ``SimulatedDesktop`` backend that keeps
an in-memory model of apps, windows and a frame buffer. The *policy* parts are
real, testable Python: grants and tiers, screenshot sizing, coordinate mapping,
the action gate, batch semantics, helper supervision timings.

Spec coverage
-------------
* 09 §2.3 grants (monotonic within a session; tiers read < click < full),
  §2.6 category tiers (browsers & trading -> read; terminals/IDEs -> click;
  others full; requested tier may only be lowered, R2.5), R2.1 (resolve names
  case-insensitively, any failure fails the whole request), R2.2 (never grant the
  host app itself), R2.3 (deny lists), R2.4 (wildcard grant needs a non-empty
  deny list), R2.6 (first-request friction for browsers/terminals).
* 09 R3.1 / R4.1 the two other consents: OS permissions (``preflight``) and
  screen takeover (``require_takeover``) - app grants never imply either (09#T2).
* 09 §4.1 ``fit`` (28 px/token, <= 1568 tokens and <= 1568 px edge, binary search
  on the long edge), §4.2 ``to_logical`` (model speaks screenshot pixels; server
  rescales and clamps; out-of-frame teaches), §4.3 action gate (frontmost grant,
  tier check, hit-test, delivery-time re-check, partial text delivery), §4.4
  batch (one coordinate reference, stop at first error, release held buttons).
* 09 R6.4 every tier/policy error ends with a ban on working around it.
* 10 §4.1 AX-first with raw fallback only for allowlisted refusal reasons;
  unknown reasons fail closed; unsupported cache 3 hits -> 5 s.
* 10 §4.3 / R7.1-R7.4 helper supervisor: timeout 8 s + 50 ms/char (>= 1 s) +
  400 ms/via-point, accumulated deadlines, restart backoff 0/1/5 s then 60 s,
  crash-loop pause 300 s, errors say whether the request reached the helper.
* Takeaways 09#T1-T13, 10#T1-T10; P26-P28.

Design choices
--------------
* ``to_logical`` without any screenshot raises ``no_screenshot`` instead of
  guessing a scale factor (fail closed; 09 §4.2 only says "warn").
* A denied app (deny lists) fails the whole ``request_access`` call, like an
  unresolvable name (one dialog for all, nothing partially granted).
* Graphemes are approximated as a base code point plus following combining marks.
* Crash-loop breaker: 5 crashes without one successful round trip; SIGKILL after
  3 consecutive timeouts (the spec gives no numbers).

Deferred (documented gaps)
--------------------------
* Teach / watch modes (R5), machine lock and Esc kill switch (R8), glow and
  phantom-cursor overlays, hide-before-action and turn-end cleanup (R7.1, R10),
  clipboard stash/restore (R7.3; only the write refusal is modelled), protected
  documents and save panels (R7.5), pixel staleness (R7.7), clipboard paste path
  for multi-line text, mouse animation timings (§4.5), installed-app list for the
  tool description (§2.5) and string sanitisation (R6.2).
"""

from __future__ import annotations

import enum
import math
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

PX_PER_TOKEN = 28
MAX_EDGE = 1568
MAX_TOKENS = 1568
JPEG_QUALITY = 0.75
TEXT_CHUNK = 512
TEXT_CAP = 4000
UNSUPPORTED_CACHE_HITS, UNSUPPORTED_CACHE_S = 3, 5.0
HELPER_BASE_TIMEOUT_S = 8.0
HELPER_BACKOFF_S = (0.0, 1.0, 5.0)
HELPER_COOLDOWN_S = 60.0
HELPER_BREAKER_PAUSE_S = 300.0
HELPER_BREAKER_CRASHES = 5
HELPER_KILL_AFTER_TIMEOUTS = 3
USER_TYPING_RETRIES, USER_TYPING_DELAY_S = 6, 0.4
MAX_REPEAT = 100
MENU_BAR = "__menubar__"
SELF_OVERLAY = "__self_overlay__"
FILE_MANAGER = "com.apple.finder"

WORKAROUND_BAN = (
    " Do not try to get around this restriction with scripting bridges, the shell, or any other route."
)


class Tier(enum.IntEnum):
    READ = 0
    CLICK = 1
    FULL = 2


ActionKind = Literal["mouse_position", "mouse", "mouse_full", "keyboard"]


@dataclass
class Grant:
    bundle_id: str
    display_name: str
    tier: Tier
    granted_at: float = 0.0


@dataclass
class Frame:
    width: int
    height: int
    display_id: int
    display_width: int
    display_height: int
    origin_x: int = 0
    origin_y: int = 0
    scale_factor: float = 2.0


@dataclass
class CUSession:
    grants: list[Grant] = field(default_factory=list)
    clipboard_read: bool = False
    clipboard_write: bool = False
    system_key_combos: bool = False
    takeover_approved: bool = False
    hidden_during_turn: list[str] = field(default_factory=list)
    last_screenshot: Frame | None = None
    coordinate_mode: Literal["pixels", "normalized_0_100"] = "pixels"
    host_bundle_id: str = "com.example.harnesslab"
    warned_categories: set[str] = field(default_factory=set)
    policy_deny: list[str] = field(default_factory=list)  # needed for a wildcard grant (R2.4)


class CUError(Exception):
    """Teaching error: ``code`` from a closed vocabulary; message names the alternative."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# ------------------------------------------------------------------ categories (09 §2.6)

BROWSER_IDS = frozenset(
    {
        "com.apple.Safari", "com.google.Chrome", "com.google.Chrome.canary", "org.chromium.Chromium",
        "org.mozilla.firefox", "com.microsoft.edgemac", "company.thebrowser.Browser", "com.brave.Browser",
        "com.operasoftware.Opera", "com.vivaldi.Vivaldi", "app.zen-browser.zen", "com.kagi.kagimacOS",
    }
)
BROWSER_WORDS = ("browser", "safari", "chrome", "firefox", "edge", "opera", "vivaldi", "brave", "chromium")
TRADING_WORDS = ("trading", "trade", "broker", "crypto", "wallet", "coinbase", "robinhood", "binance", "etrade", "schwab", "fidelity", "webull", "kraken", "metamask", "ledger live")
CLICK_TIER_IDS = frozenset(
    {
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.mitchellh.ghostty", "io.alacritty",
        "net.kovidgoyal.kitty", "com.microsoft.VSCode", "com.todesktop.230313mzl4w4u92", "dev.zed.Zed",
        "com.sublimetext.4", "com.apple.dt.Xcode", "com.apple.ScriptEditor2", "com.apple.Automator",
        "com.apple.shortcuts", "explorer.exe", "SearchHost.exe", "StartMenuExperienceHost.exe",
    }
)
CLICK_TIER_PREFIXES = ("com.jetbrains.",)
CLICK_WORDS = ("terminal", "iterm", "console", "code", "xcode", "intellij", "pycharm", "script editor", "automator")
POLICY_DENY_SUBSTRINGS = ("netflix", "disney+", "prime video", "hbo max", "apple tv")
SYSTEM_COMBOS = frozenset({"cmd+q", "cmd+tab", "cmd+space", "cmd+w", "cmd+option+esc", "ctrl+cmd+q", "ctrl+left", "ctrl+right", "ctrl+up", "cmd+h", "cmd+m"})


def category_of(bundle_id: str, name: str) -> str:
    n = name.lower()
    if bundle_id in BROWSER_IDS or any(w in n for w in BROWSER_WORDS):
        return "browser"
    if any(w in n or w in bundle_id.lower() for w in TRADING_WORDS):
        return "trading"
    if bundle_id in CLICK_TIER_IDS or bundle_id.startswith(CLICK_TIER_PREFIXES) or any(w == n or n.startswith(w) for w in CLICK_WORDS):
        return "terminal"
    return "other"


def category_tier(bundle_id: str, name: str) -> Tier:
    return {"browser": Tier.READ, "trading": Tier.READ, "terminal": Tier.CLICK}.get(category_of(bundle_id, name), Tier.FULL)


# ------------------------------------------------------------------ pure policy


def screenshot_tokens(w: int, h: int) -> int:
    """09 §4.1: one image token per 28x28 px tile (rounded up per axis)."""
    return math.ceil(w / PX_PER_TOKEN) * math.ceil(h / PX_PER_TOKEN)


def _dims_for(long_edge: int, aspect: float) -> tuple[int, int]:
    if aspect >= 1:
        return long_edge, max(1, round(long_edge / aspect))
    return max(1, round(long_edge * aspect)), long_edge


def fit(phys_w: int, phys_h: int) -> tuple[int, int]:
    """Largest size with the same aspect ratio within the edge and token caps (09 §4.1, 09#T8)."""
    if max(phys_w, phys_h) <= MAX_EDGE and screenshot_tokens(phys_w, phys_h) <= MAX_TOKENS:
        return phys_w, phys_h
    aspect = phys_w / phys_h
    lo, hi = 1, max(phys_w, phys_h)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        w, h = _dims_for(mid, aspect)
        if mid <= MAX_EDGE and screenshot_tokens(w, h) <= MAX_TOKENS:
            lo = mid
        else:
            hi = mid - 1
    return _dims_for(lo, aspect)


def to_logical(x: float, y: float, sess: CUSession) -> tuple[int, int]:
    """Screenshot pixels -> global logical points (09 §4.2, 09#T7)."""
    f = sess.last_screenshot
    if f is None:
        raise CUError("no_screenshot", "Take a screenshot first: coordinates refer to the most recent screenshot.")
    if sess.coordinate_mode == "normalized_0_100":
        x, y = x / 100 * f.width, y / 100 * f.height
    if not (0 <= x < f.width and 0 <= y < f.height):
        raise CUError(
            "coordinate_out_of_frame",
            f"Coordinate ({x:g}, {y:g}) is outside the last screenshot, which is {f.width}x{f.height} pixels. "
            "Use coordinates inside that image (0 <= x < width, 0 <= y < height).",
        )
    lx = round(x * f.display_width / f.width) + f.origin_x
    ly = round(y * f.display_height / f.height) + f.origin_y
    lx = min(max(lx, f.origin_x), f.origin_x + f.display_width - 1)
    ly = min(max(ly, f.origin_y), f.origin_y + f.display_height - 1)
    return lx, ly


ACTION_KINDS: dict[str, ActionKind] = {
    "screenshot": "mouse_position", "zoom": "mouse_position", "cursor_position": "mouse_position", "wait": "mouse_position",
    "left_click": "mouse", "scroll": "mouse", "mouse_move": "mouse",
    "double_click": "mouse_full", "triple_click": "mouse_full", "right_click": "mouse_full", "middle_click": "mouse_full",
    "left_click_drag": "mouse_full", "left_mouse_down": "mouse_full", "left_mouse_up": "mouse_full",
    "type": "keyboard", "key": "keyboard", "hold_key": "keyboard", "write_clipboard": "keyboard",
}


def required_kind(action: str) -> ActionKind:
    try:
        return ACTION_KINDS[action]
    except KeyError:
        raise CUError("unknown_action", f"Unknown action {action!r}.") from None


def tier_allows(tier: Tier, kind: ActionKind) -> bool:
    if kind == "mouse_position":
        return True
    if kind == "mouse":
        return tier >= Tier.CLICK
    return tier >= Tier.FULL


def _tier_error(app: str, tier: Tier, action: str) -> CUError:
    if tier == Tier.READ:
        allowed = "screenshots only"
        better = "Use browser automation tools for web pages instead." if category_of(app, app) == "browser" else "Read the screen and tell the user what you see."
    else:
        allowed = "plain left clicks and scrolling only"
        better = "Use the shell tool for terminal or editor work instead of typing into this app."
    return CUError("insufficient_tier", f"{app} is granted at '{tier.name.lower()}' tier ({allowed}); '{action}' is not allowed. {better}{WORKAROUND_BAN}")


def grant_for(sess: CUSession, bundle_id: str, name: str = "") -> Grant | None:
    """The effective grant for an app (wildcard grants still respect category tiers and deny lists)."""
    g = next((g for g in sess.grants if g.bundle_id == bundle_id), None)
    if g is not None:
        return g
    if any(g.bundle_id == "*" for g in sess.grants):
        if bundle_id == sess.host_bundle_id or _denied(bundle_id, name or bundle_id, sess.policy_deny):
            return None
        return Grant(bundle_id, name or bundle_id, category_tier(bundle_id, name or bundle_id))
    return None


def _denied(bundle_id: str, name: str, deny: list[str]) -> bool:
    n, b = name.lower(), bundle_id.lower()
    if bundle_id.startswith("<"):
        return True
    if any(s in n for s in POLICY_DENY_SUBSTRINGS):
        return True
    return any(d.lower() in (n, b) for d in deny)


def request_access(sess: CUSession, apps: list[str], installed: dict[str, str], *, deny_list: list[str] | None = None, requested: Tier = Tier.FULL, now: float = 0.0) -> list[Grant]:
    """09 R2.1-R2.6. ``installed`` maps display name -> bundle id."""
    deny = list(deny_list or [])
    if apps == ["*"]:
        if not (deny or sess.policy_deny):
            raise ValueError("a wildcard grant requires a non-empty policy deny list")  # R2.4: programming error
        sess.policy_deny = sorted(set(sess.policy_deny) | set(deny))
        g = Grant("*", "All applications", Tier.FULL, now)
        if not any(x.bundle_id == "*" for x in sess.grants):
            sess.grants.append(g)
        return [g]
    by_lower = {k.lower(): (k, v) for k, v in installed.items()}
    ids = {v: k for k, v in installed.items()}
    resolved: list[tuple[str, str]] = []
    unknown: list[str] = []
    for a in apps:
        hit = by_lower.get(a.strip().lower())
        if hit is not None:
            resolved.append((hit[1], hit[0]))
        elif a in ids:
            resolved.append((a, ids[a]))
        else:
            unknown.append(a)
    if unknown:
        from harnesslab.extensions.skills import edit_distance  # own WP; pure helper

        hints = []
        for u in unknown:
            near = [k for k in installed if edit_distance(u.lower(), k.lower()) <= 3]
            hints.append(f"{u!r}" + (f" (did you mean {', '.join(near)}?)" if near else ""))
        raise CUError("unknown_app", f"Could not find these apps: {'; '.join(hints)}. Nothing was granted; fix the names and request again.")
    for bid, name in resolved:
        if bid == sess.host_bundle_id:
            raise CUError("self_grant_refused", f"{name} is the agent's own app and can never be granted (it would let the agent change its own permissions).")
        if _denied(bid, name, deny):
            raise CUError("app_denied", f"{name} is on a deny list and cannot be controlled. Nothing was granted.{WORKAROUND_BAN}")
    for bid, name in resolved:  # R2.6 first-request friction, before any dialog
        cat = category_of(bid, name)
        if cat in ("browser", "terminal") and cat not in sess.warned_categories:
            sess.warned_categories.add(cat)
            what = "a web browser (read-only: screenshots only)" if cat == "browser" else "a terminal or code editor (clicks and scrolling only)"
            better = "browser automation tools" if cat == "browser" else "the shell tool"
            raise CUError(
                "first_request_warning",
                f"{name} is {what}. {better.capitalize()} are usually the better choice. If you truly still need it, "
                "call request_access again in this same turn.",
            )
    out = []
    for bid, name in resolved:
        tier = min(requested, category_tier(bid, name))  # lower-only (R2.5)
        existing = grant_for(sess, bid, name) if any(g.bundle_id == bid for g in sess.grants) else None
        if existing is not None:
            existing.tier = max(existing.tier, tier)  # grants are monotonic within a session
            out.append(existing)
        else:
            g = Grant(bid, name, tier, now)
            sess.grants.append(g)
            out.append(g)
    return out


@dataclass
class OsPermissions:
    accessibility: bool = False
    screen_recording: bool = False


REQUEST_TOOLS = frozenset({"request_access", "request_teach_access"})


def preflight(sess: CUSession, perms: OsPermissions, tool: str) -> None:
    """09 R3.1 / R11.1: OS permissions come first; an empty allowlist blocks input tools."""
    if tool in REQUEST_TOOLS:
        return
    if not (perms.accessibility and perms.screen_recording):
        missing = [n for n, ok in (("Accessibility", perms.accessibility), ("Screen Recording", perms.screen_recording)) if not ok]
        raise CUError("os_permission_missing", f"The OS permission(s) {', '.join(missing)} are not granted. The permission panel appears when you invoke request_access.")
    if not sess.grants:
        raise CUError("no_apps_granted", "No applications are granted yet. Call request_access with the apps you need first.")


def require_takeover(sess: CUSession, ask: Callable[[], Literal["approved", "declined", "timeout"]]) -> None:
    """09 R4.1: full-screen takeover is a separate consent from app grants."""
    if sess.takeover_approved:
        return
    answer = ask()
    if answer == "approved":
        sess.takeover_approved = True
        return
    if answer == "declined":
        raise CUError("takeover_declined", "The user declined screen takeover. Use app-scope tools or ask the user how to proceed.")
    raise CUError("takeover_not_answered", "Nobody answered the screen-takeover request. Do not retry this turn.")


def graphemes(text: str) -> list[str]:
    out: list[str] = []
    for ch in text:
        if out and unicodedata.combining(ch):
            out[-1] += ch
        else:
            out.append(ch)
    return out


# ------------------------------------------------------------------ native seams


class ScreenCapture(Protocol):
    def capture_excluding(self, allowed_bundle_ids: list[str], out_w: int, out_h: int, display_id: int | None = None) -> tuple[bytes, Frame]: ...

    def display_size(self, display_id: int | None = None) -> tuple[int, int, float]:
        """``(logical_width, logical_height, scale_factor)``."""
        ...


class InputInjector(Protocol):
    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None: ...

    def type_text(self, text: str) -> int:
        """Return number of characters actually delivered."""
        ...

    def key(self, chord: str) -> None: ...

    def move(self, x: int, y: int) -> None: ...

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...

    def button(self, pressed: bool) -> None: ...


class AxDriver(Protocol):
    def frontmost_app(self) -> str: ...

    def app_under_point(self, x: int, y: int) -> str | None: ...

    def probe(self, bundle_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        """``{"outcome": "ok"|"ineffective"|"unsupported", "reason": str}`` (10 §2.3)."""
        ...

    def window_owner(self, window_id: int) -> str | None: ...

    def session_guard(self) -> dict[str, Any]:
        """``{"secure_input_pid": int | None, "screen_locked": bool}``."""
        ...


class RawEventHelper(Protocol):
    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class SimulatedDesktop:
    """In-memory stand-in implementing all four seams for tests and demos."""

    def __init__(self, apps: dict[str, str] | None = None, width: int = 1440, height: int = 900) -> None:
        self.apps = apps or {}  # bundle id -> display name
        self.width, self.height = width, height
        self.frontmost: str | None = None
        self.events: list[tuple[str, Any]] = []
        self.windows: dict[str, tuple[int, int, int, int]] = {}  # bundle -> rect
        self.scale_factor = 2.0
        self.typed = ""
        self.focus_script: dict[int, str] = {}  # after N delivered chars, frontmost becomes X
        self.probe_results: dict[str, dict[str, Any]] = {}  # intent kind -> probe result
        self.window_owners: dict[int, str] = {}
        self.guard: dict[str, Any] = {"secure_input_pid": None, "screen_locked": False}
        self.frame_bytes: int = 4096
        self.held = False

    def display_size(self, display_id: int | None = None) -> tuple[int, int, float]:
        return self.width, self.height, self.scale_factor

    def capture_excluding(self, allowed_bundle_ids: list[str], out_w: int, out_h: int, display_id: int | None = None) -> tuple[bytes, Frame]:
        visible = [b for b in self.windows if b in allowed_bundle_ids]
        self.events.append(("capture", (tuple(visible), out_w, out_h)))
        data = (b"SIMJPEG:" + ",".join(visible).encode()).ljust(self.frame_bytes, b"\0")
        return data, Frame(out_w, out_h, display_id or 1, self.width, self.height, 0, 0, self.scale_factor)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.events.append(("click", (x, y, button, count)))

    def type_text(self, text: str) -> int:
        self.typed += text
        self.events.append(("type", text))
        if len(self.typed) in self.focus_script:
            self.frontmost = self.focus_script[len(self.typed)]
        return len(text)

    def key(self, chord: str) -> None:
        self.events.append(("key", chord))

    def move(self, x: int, y: int) -> None:
        self.events.append(("move", (x, y)))

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        self.events.append(("scroll", (x, y, direction, amount)))

    def button(self, pressed: bool) -> None:
        self.held = pressed
        self.events.append(("button", pressed))

    def frontmost_app(self) -> str:
        return self.frontmost or ""

    def app_under_point(self, x: int, y: int) -> str | None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        if y < 25:
            return MENU_BAR
        for bundle, (wx, wy, ww, wh) in reversed(list(self.windows.items())):  # last added = topmost
            if wx <= x < wx + ww and wy <= y < wy + wh:
                return bundle
        return "com.apple.finder"  # the desktop belongs to the file manager

    def probe(self, bundle_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("probe", (bundle_id, intent.get("kind"))))
        return dict(self.probe_results.get(str(intent.get("kind")), {"outcome": "ok"}))

    def window_owner(self, window_id: int) -> str | None:
        return self.window_owners.get(window_id)

    def session_guard(self) -> dict[str, Any]:
        return dict(self.guard)


# ------------------------------------------------------------------ action gate (09 §4.3-§4.4)


class ActionGate:
    """09 §4.3 ``run_input_action`` over the seams."""

    def __init__(self, sess: CUSession, capture: ScreenCapture, injector: InputInjector, ax: AxDriver) -> None:
        self.sess, self.capture, self.injector, self.ax = sess, capture, injector, ax
        self.notes: list[str] = []

    def screenshot(self, scale: float = 1.0) -> tuple[bytes, Frame]:
        """Capture at the token-budget size; only granted apps are visible (09 §4.1, R9.1)."""
        w_log, h_log, sf = self.capture.display_size()
        w, h = fit(round(w_log * sf), round(h_log * sf))
        allowed = [g.bundle_id for g in self.sess.grants if g.bundle_id not in ("*", self.sess.host_bundle_id)]
        img, frame = self.capture.capture_excluding(allowed, w, h)
        if len(img) < max(64, 1024 * scale**2):  # implausibly small frame: retry once
            img, frame = self.capture.capture_excluding(allowed, w, h)
        self.sess.last_screenshot = frame  # the coordinate frame stays w x h even when scale < 1
        if scale < 1:
            self.notes.append(f"{scale}-scale view; coordinates still refer to a {w}x{h} frame")
        return img, frame

    def _gate_app(self, bundle: str, action: str) -> Grant:
        g = grant_for(self.sess, bundle, self.ax_name(bundle))
        if g is None:
            raise CUError("app_not_granted", f"{bundle or 'The frontmost app'} is not granted. Take a new screenshot to see what is in front, or request access to it.{WORKAROUND_BAN}")
        if not tier_allows(g.tier, required_kind(action)):
            raise _tier_error(g.display_name or bundle, g.tier, action)
        return g

    def ax_name(self, bundle: str) -> str:
        apps = getattr(self.ax, "apps", {}) or {}
        return apps.get(bundle, bundle)

    def run(self, action: str, **args: Any) -> dict[str, Any]:
        kind = required_kind(action)
        if kind == "mouse_position" and action in ("screenshot", "zoom"):
            img, frame = self.screenshot(float(args.get("scale", 1.0)))
            return {"ok": True, "action": action, "frame": (frame.width, frame.height), "bytes": len(img)}
        front = self.ax.frontmost_app()
        if front == self.sess.host_bundle_id:
            raise CUError("state_conflict", "The agent's own window is in front. Switch to a granted app first.")
        g = self._gate_app(front, action)

        if action == "write_clipboard":
            if g.tier == Tier.CLICK or not self.sess.clipboard_write:
                raise CUError("clipboard_denied", f"Writing the clipboard is not allowed here.{WORKAROUND_BAN}")
        if action == "key":
            chord = str(args.get("text", "")).lower().replace(" ", "")
            if chord in SYSTEM_COMBOS and not self.sess.system_key_combos:
                raise CUError("system_combo_denied", f"'{chord}' is a system key combination and needs the system-combos grant.{WORKAROUND_BAN}")

        point = None
        if "coordinate" in args and args["coordinate"] is not None:
            x, y = args["coordinate"]
            point = to_logical(x, y, self.sess)
            under = self.ax.app_under_point(*point)
            if under is None:
                raise CUError("hit_test_off_display", "That point is not on any display. Take a new screenshot.")
            if under == SELF_OVERLAY:
                raise CUError("self_intercept", "The agent's own overlay is at that point. Retry the action.")
            if under == MENU_BAR:
                under = front
            if under != front:
                self._gate_app(under, action)  # enforce the tier of the app actually hit
        return self._deliver(action, args, point, front)

    def _still_front(self, approved: str) -> bool:
        return self.ax.frontmost_app() == approved

    def _deliver(self, action: str, args: dict[str, Any], point: tuple[int, int] | None, approved_front: str) -> dict[str, Any]:
        inj = self.injector
        if action in ("left_click", "double_click", "triple_click", "right_click", "middle_click"):
            assert point is not None, "click needs a coordinate"
            button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
            count = {"double_click": 2, "triple_click": 3}.get(action, 1)
            inj.click(*point, button=button, count=count)
        elif action == "mouse_move":
            inj.move(*point)  # type: ignore[misc]
        elif action == "scroll":
            inj.scroll(*point, str(args.get("scroll_direction", "down")), min(int(args.get("scroll_amount", 3)), MAX_REPEAT))  # type: ignore[misc]
        elif action == "left_mouse_down":
            inj.button(True)
        elif action == "left_mouse_up":
            inj.button(False)
        elif action == "left_click_drag":
            start = to_logical(*args["start_coordinate"], self.sess) if args.get("start_coordinate") else None
            if start:
                inj.move(*start)
            inj.button(True)
            try:
                inj.move(*point)  # type: ignore[misc]
            finally:
                inj.button(False)
        elif action == "type":
            return self._deliver_text(str(args.get("text", "")), approved_front)
        elif action == "key":
            reps = min(int(args.get("repeat", 1)), MAX_REPEAT)
            for i in range(reps):
                if not self._still_front(approved_front):  # 09 R7.6 delivery-time re-check
                    return {"ok": False, "partial": True, "delivered": i, "message": f"The frontmost app changed; pressed {i} of {reps} times, then stopped."}
                inj.key(str(args.get("text", "")))
        return {"ok": True, "action": action, "point": point}

    def _deliver_text(self, text: str, approved_front: str) -> dict[str, Any]:
        """Grapheme by grapheme with a frontmost re-check before each (09 §4.3, 09#T4)."""
        typed = ""
        for g in graphemes(text):
            if not self._still_front(approved_front):
                return {
                    "ok": False,
                    "partial": True,
                    "delivered": typed,
                    "message": f"The frontmost app changed while typing. Only {len(typed)} characters were typed: {typed!r}. Do not retype them.",
                }
            self.injector.type_text(g)
            typed += g
        return {"ok": True, "action": "type", "delivered": typed}

    def batch(self, actions: list[dict[str, Any]]) -> list[str]:
        """One coordinate reference for the whole batch; stop at the first error (09 §4.4)."""
        ref = self.sess.last_screenshot
        results: list[str] = []
        n = len(actions)
        try:
            for i, a in enumerate(actions):
                a = dict(a)
                name = a.pop("action", "?")
                self.sess.last_screenshot = ref
                try:
                    r = self.run(name, **a)
                except CUError as e:
                    results += [f"[{i + 1}/{n}] {name}: FAILED {e}", f"stopped at actions[{i}]"]
                    return results
                if not r.get("ok", True):
                    results += [f"[{i + 1}/{n}] {name}: FAILED {r.get('message', '')}", f"stopped at actions[{i}]"]
                    return results
                results.append(f"[{i + 1}/{n}] {name}: ok")
                if name == "screenshot":
                    ref = self.sess.last_screenshot  # a full screenshot becomes the new reference
        finally:
            if getattr(self.injector, "held", False):
                self.injector.button(False)  # never leave a mouse button pressed (09 R10.1)
        return results


# ------------------------------------------------------------------ app scope: AX first (10 §4.1)

#: Refusal reasons from the AX layer and whether a raw per-process event may be tried.
RAW_FALLBACK_REASONS: dict[str, Literal["raw_ok", "block"]] = {
    "toolkit_ignores_background_drag": "raw_ok",
    "ax_write_noop": "raw_ok",
    "focus_did_not_follow": "raw_ok",
    "element_not_actionable": "raw_ok",
    "secure_field": "block",
    "foreign_process_owns_element": "block",
    "window_on_other_space": "block",
    "window_minimised": "block",
    "app_hidden": "block",
    "sheet_dimmed_area": "block",
    "popup_menu_open": "block",
    "context_menu_open": "block",
    "element_covered": "block",
    "user_active_in_app": "block",
    "compositor_transform_active": "block",
    "recent_hardware_keydown": "block",
    "secure_input_started": "block",
}


class AppScopeDriver:
    """Semantic AX actions first; classified raw fallback; unknown reasons fail closed (10#T4)."""

    def __init__(self, ax: AxDriver, helper: RawEventHelper, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ax, self.helper, self.clock, self.sleep = ax, helper, clock, sleep
        self.unsupported: dict[tuple[int, str], tuple[int, float, str]] = {}

    def act(self, bundle_id: str, window_id: int, intent: dict[str, Any]) -> dict[str, Any]:
        key = (window_id, str(intent.get("kind")))
        hits, until, last_reason = self.unsupported.get(key, (0, 0.0, ""))
        if hits >= UNSUPPORTED_CACHE_HITS and self.clock() < until:
            r = {"outcome": "unsupported", "reason": last_reason}
        else:
            r = self.ax.probe(bundle_id, intent)
        outcome = r.get("outcome")
        if outcome == "ok":
            return r
        if outcome == "unsupported":
            reason = str(r.get("reason", ""))
            self.unsupported[key] = (hits + 1, self.clock() + UNSUPPORTED_CACHE_S, reason)
            if RAW_FALLBACK_REASONS.get(reason) == "raw_ok":
                if self.ax.window_owner(window_id) != bundle_id:
                    raise CUError("owner_mismatch", "That window does not belong to the granted app; nothing was sent.")
                guard = self.ax.session_guard()
                if guard.get("secure_input_pid") or guard.get("screen_locked"):
                    raise CUError("session_guard", "Secure input is active or the screen is locked; nothing was sent.")
                return self._raw(bundle_id, window_id, intent)
            raise CUError(f"blocked:{reason or 'unknown'}", f"The action was refused ({reason or 'unknown reason'}).{WORKAROUND_BAN}")
        raise CUError("ineffective", "The action ran but had no visible effect. Take a screenshot and decide what to do next.")

    def _raw(self, bundle_id: str, window_id: int, intent: dict[str, Any]) -> dict[str, Any]:
        params = {"windowId": window_id, "bundleId": bundle_id, **intent}
        for attempt in range(USER_TYPING_RETRIES + 1):
            r = self.helper.call("dispatchRaw", params)
            if r.get("error") != "user_typing":
                return r
            if attempt < USER_TYPING_RETRIES:
                self.sleep(USER_TYPING_DELAY_S)
        raise CUError("user_typing", "The user is typing in that app. Ask them before continuing.")


def text_chunks(text: str, size: int = TEXT_CHUNK, cap: int = TEXT_CAP) -> list[str]:
    """Slices of at most ``size`` UTF-16 units that never split a surrogate pair (10 R3.3)."""
    text = text[:cap]
    chunks, cur, units = [], "", 0
    for ch in text:
        u = 2 if ord(ch) > 0xFFFF else 1
        if units + u > size:
            chunks.append(cur)
            cur, units = "", 0
        cur += ch
        units += u
    if cur:
        chunks.append(cur)
    return chunks


# ------------------------------------------------------------------ helper supervisor (10 §4.3)


class HelperError(Exception):
    """Says whether the request reached the helper, i.e. whether a side effect may have happened (10 R7.4)."""

    def __init__(self, message: str, *, reached: bool) -> None:
        where = "the request reached the helper, so it may have had an effect" if reached else "the request never reached the helper; nothing happened"
        super().__init__(f"{message} ({where})")
        self.reached = reached


@dataclass
class HelperSupervisor:
    """10 §4.3 timing/backoff policy (pure; no process is spawned)."""

    queue_tail_deadline: float = 0.0
    crashes: int = 0
    crashes_without_roundtrip: int = 0
    consecutive_timeouts: int = 0

    def request_budget(self, method: str, params: dict[str, Any]) -> float:
        budget = HELPER_BASE_TIMEOUT_S
        if method == "dispatchRaw" and params.get("text"):
            budget += max(1.0, 0.05 * len(params["text"]))
        budget += 0.4 * len(params.get("viaWinLocalPts") or [])
        return budget

    def next_deadline(self, now: float, method: str, params: dict[str, Any]) -> float:
        """Deadlines accumulate: a request queued behind others gets their time too."""
        deadline = max(self.queue_tail_deadline, now) + self.request_budget(method, params)
        self.queue_tail_deadline = deadline
        return deadline

    def on_success(self) -> None:
        self.crashes_without_roundtrip = 0
        self.consecutive_timeouts = 0

    def on_timeout(self) -> bool:
        """Count a timeout; True means "kill the helper now"."""
        self.consecutive_timeouts += 1
        return self.consecutive_timeouts >= HELPER_KILL_AFTER_TIMEOUTS

    def on_exit(self) -> None:
        self.crashes += 1
        self.crashes_without_roundtrip += 1

    def respawn_delay(self) -> float:
        """0 / 1 / 5 s, then a 60 s cooldown; a crash loop pauses respawns for 300 s."""
        if self.crashes_without_roundtrip >= HELPER_BREAKER_CRASHES:
            return HELPER_BREAKER_PAUSE_S
        if self.crashes <= 0:
            return 0.0
        if self.crashes <= len(HELPER_BACKOFF_S):
            return HELPER_BACKOFF_S[self.crashes - 1]
        return HELPER_COOLDOWN_S

    @staticmethod
    def cancel_plan(pending: int) -> str:
        """10 R7.3: more than one pending request -> kill now; else close stdin with 0.7 s grace."""
        return "sigkill" if pending > 1 else "close_stdin_then_kill_after_0.7s"
