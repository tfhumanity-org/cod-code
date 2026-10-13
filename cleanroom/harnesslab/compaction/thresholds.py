"""Window resolution and threshold levels (WP4).

Implements spec 05 §2.2, R5 (window precedence: ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
clamped to [100k, 1M] > per-model setting > server > experiment > model
default > advertised), R6 + §4.1 (levels from the effective window:
reserve = min(max_output, 20k); threshold = effective - 13k (optionally
``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE``); warn = threshold - 20k; blocked =
context_window - reserve - 3k (or ``CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE``);
precompute = min(effective - 20 %, threshold)), R7 (``DISABLE_COMPACT`` /
``DISABLE_AUTO_COMPACT`` / ``autoCompactEnabled``), R9 (UI percentage text);
01 R49 (blocking level - architect gap G5 says use the 05 §4.1 formula);
takeaway 05#T7.

Worked example from the spec (a unit test): window 200k, max output
64k -> effective 180k, threshold 167k, warn 147k, precompute 144k, blocked 177k.

Design choices (spec silent):

* "server" and "experiment" configuration have no transport in this library, so
  they are read from the settings keys ``serverConfig.autoCompactWindow`` and
  ``experiments.autoCompactWindow`` (a host can inject them there).
* "model default": windows above 200k are clamped to 200k unless the model id
  carries the ``[1m]`` opt-in suffix (05 R5 item 5 says such models are clamped).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping

from harnesslab.config import Settings, env_falsy, env_float, env_int, env_truthy

OUTPUT_RESERVE_CAP = 20_000
AUTOCOMPACT_BUFFER = 13_000
WARN_GAP = 20_000
BLOCK_BUFFER = 3_000
PRECOMPUTE_FRAC = 0.20
WINDOW_ENV_MIN = 100_000
WINDOW_ENV_MAX = 1_000_000
DEFAULT_WINDOW = 200_000


@dataclass
class WindowInfo:
    window: int
    configured: bool
    source: Literal["env", "settings", "server", "experiment", "model-default", "auto", "unknown-model"]


@dataclass
class Levels:
    effective: int
    warn: int
    threshold: int
    blocked: int
    precompute: int
    state: Literal["ok", "warn", "compact", "blocked"]
    pct_left: float


def _positive_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    return None


def resolve_window(env: Mapping[str, str], settings: Settings, *, model_id: str, model_window: int) -> WindowInfo:
    """Resolve the usable context window with the 05 R5 precedence."""
    # (1) env var, clamped
    if env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "").strip():
        w = env_int(env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW", DEFAULT_WINDOW, lo=WINDOW_ENV_MIN, hi=WINDOW_ENV_MAX)
        return WindowInfo(w, True, "env")
    # (2) user setting: per model, falling back to a "default" entry (or a bare int)
    s = settings.get("autoCompactWindow")
    if isinstance(s, Mapping):
        w = _positive_int(s.get(model_id)) or _positive_int(s.get("default"))
        if w:
            return WindowInfo(w, True, "settings")
    elif _positive_int(s):
        return WindowInfo(int(s), True, "settings")  # type: ignore[arg-type]
    # (3) server-delivered, (4) experiment
    for key, source in (("serverConfig.autoCompactWindow", "server"), ("experiments.autoCompactWindow", "experiment")):
        w = _positive_int(settings.get(key))
        if w:
            return WindowInfo(w, False, source)  # type: ignore[arg-type]
    # (5) model default / (6) advertised
    if model_window <= 0:
        return WindowInfo(DEFAULT_WINDOW, False, "unknown-model")
    if model_window > DEFAULT_WINDOW and "[1m]" not in model_id:
        return WindowInfo(DEFAULT_WINDOW, False, "model-default")
    return WindowInfo(model_window, False, "auto")


def levels(
    window: int,
    context_window: int,
    max_output: int,
    tokens: int,
    *,
    pct_override: float | None = None,
    block_override: int | None = None,
) -> Levels:
    """05 §4.1 verbatim: every level derives from the *effective* window."""
    reserve = min(max_output, OUTPUT_RESERVE_CAP)
    effective = window - reserve
    threshold = effective - AUTOCOMPACT_BUFFER
    if pct_override is not None:
        threshold = min(math.floor(effective * pct_override / 100), threshold)
    warn = threshold - WARN_GAP
    blocked = block_override or (context_window - reserve - BLOCK_BUFFER)
    precompute = min(effective - round(effective * PRECOMPUTE_FRAC), threshold)
    if tokens >= blocked:
        state: Literal["ok", "warn", "compact", "blocked"] = "blocked"
    elif tokens >= threshold:
        state = "compact"
    elif tokens >= warn:
        state = "warn"
    else:
        state = "ok"
    pct_left = (threshold - tokens) / threshold if threshold > 0 else 0.0
    return Levels(effective, warn, threshold, blocked, precompute, state, pct_left)


def levels_from_env(
    env: Mapping[str, str], window: int, context_window: int, max_output: int, tokens: int
) -> Levels:
    """``levels`` with the two env overrides of 05 §5 applied."""
    pct = env_float(env, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", -1.0)
    block = env_int(env, "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE", 0)
    return levels(
        window,
        context_window,
        max_output,
        tokens,
        pct_override=pct if 0 < pct <= 100 else None,
        block_override=block if block > 0 else None,
    )


def compaction_disabled(env: Mapping[str, str]) -> bool:
    """05 R7: ``DISABLE_COMPACT`` turns off *all* compaction, manual included."""
    return env_truthy(env, "DISABLE_COMPACT")


def auto_compaction_disabled(env: Mapping[str, str], settings: Settings) -> bool:
    """05 R7: only the automatic path (manual ``/compact`` still works)."""
    if compaction_disabled(env) or env_truthy(env, "DISABLE_AUTO_COMPACT"):
        return True
    if env_falsy(env, "DISABLE_AUTO_COMPACT"):
        return False
    return settings.get("autoCompactEnabled", True) is False


def status_text(lv: Levels, *, enforced: bool) -> str:
    """05 R9: "N% until auto-compact" when enforced, "N% context used" otherwise,
    plus a low-context hint once past ``warn``."""
    if enforced:
        pct = max(0, round(lv.pct_left * 100))
        text = f"{pct}% until auto-compact"
    else:
        used_tokens = lv.threshold * (1 - lv.pct_left)
        pct = min(100, max(0, round(100 * used_tokens / lv.effective))) if lv.effective > 0 else 100
        text = f"{pct}% context used"
    if lv.state != "ok":
        text += " - context is getting low; run /compact to summarize the conversation now"
    return text
