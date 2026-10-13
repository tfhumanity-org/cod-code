"""Settings and environment knobs (FINAL - architect).

Every spec file lists env vars and settings keys in its §5. Rather than read
``os.environ`` all over the code base, modules receive an ``env`` mapping (from
``SessionContext.env``) and use the helpers here. Tests therefore never need to
monkeypatch the process environment.

* ``Settings`` - merged settings with dotted-path lookup. Sources are merged in
  increasing precedence: user < project < local < flag < policy (04 §2.3 rule
  sources, 06 §2.5 collision precedence). Lists under ``permissions.allow`` /
  ``ask`` / ``deny`` are *concatenated* (rules aggregate across sources, 04 §2.3);
  everything else is last-wins.
* ``SettingsSource`` - which file a value came from. ``Settings.source_of`` lets
  the permission layer ignore repository-controlled bypass (04 R1).
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class SettingsSource(str, enum.Enum):
    USER = "userSettings"
    PROJECT = "projectSettings"
    LOCAL = "localSettings"
    FLAG = "flagSettings"
    POLICY = "policySettings"


#: Increasing precedence (later wins).
SOURCE_ORDER = [
    SettingsSource.USER,
    SettingsSource.PROJECT,
    SettingsSource.LOCAL,
    SettingsSource.FLAG,
    SettingsSource.POLICY,
]

#: Dotted keys whose list values accumulate across sources instead of overriding.
CONCAT_KEYS = {
    "permissions.allow",
    "permissions.ask",
    "permissions.deny",
    "permissions.additionalDirectories",
    "claudeMdExcludes",
}


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v, dotted + ".")
        elif dotted in CONCAT_KEYS and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + list(v)
        elif isinstance(v, Mapping):
            out[k] = _deep_merge({}, v, dotted + ".")
        else:
            out[k] = v
    return out


@dataclass
class Settings:
    """Merged settings plus per-source raw layers."""

    layers: dict[SettingsSource, dict[str, Any]] = field(default_factory=dict)
    merged: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_layers(cls, layers: Mapping[SettingsSource, Mapping[str, Any]]) -> "Settings":
        merged: dict[str, Any] = {}
        for src in SOURCE_ORDER:
            if src in layers:
                merged = _deep_merge(merged, layers[src])
        return cls({k: dict(v) for k, v in layers.items()}, merged)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: SettingsSource = SettingsSource.USER) -> "Settings":
        return cls.from_layers({source: data})

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.merged
        for part in dotted.split("."):
            if not isinstance(cur, Mapping) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_from(self, source: SettingsSource, dotted: str, default: Any = None) -> Any:
        cur: Any = self.layers.get(source, {})
        for part in dotted.split("."):
            if not isinstance(cur, Mapping) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def source_of(self, dotted: str) -> SettingsSource | None:
        """Highest-precedence source defining ``dotted`` (04 R1)."""
        sentinel = object()
        for src in reversed(SOURCE_ORDER):
            if self.get_from(src, dotted, sentinel) is not sentinel:
                return src
        return None


def load_settings_files(paths: Mapping[SettingsSource, Path]) -> tuple[Settings, list[str]]:
    """Load JSON settings files. Returns ``(settings, errors)``.

    A file that fails to parse is reported in ``errors`` (and skipped). Callers that
    need the value to be knowable - e.g. retention sweeps (05 R49) - must check
    ``errors`` and refuse to act.
    """
    layers: dict[SettingsSource, dict[str, Any]] = {}
    errors: list[str] = []
    for src, p in paths.items():
        if not p.exists():
            continue
        try:
            layers[src] = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"{p}: {e}")
    return Settings.from_layers(layers), errors


# ----------------------------------------------------------------- env helpers

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def env_truthy(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in _TRUTHY


def env_falsy(env: Mapping[str, str], name: str) -> bool:
    """Explicitly set to a false value (distinct from unset, e.g. 02 R23)."""
    return env.get(name, "").strip().lower() in _FALSY


def env_int(
    env: Mapping[str, str], name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Integer env var with optional clamping; unparsable values fall back to default."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = int(float(raw))
    except ValueError:
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default
