"""Markdown frontmatter parser - a tiny YAML subset (FINAL - architect).

Used by memory files (02 §2.6), agent definitions (06 §2.5), SKILL.md (07 §2.2),
output styles and conditional rules (02 R19 ``paths:``). The standard library has
no YAML parser, so we support exactly what those files need:

* ``key: scalar`` (strings, ints, floats, true/false, null, quoted strings)
* ``key: [a, b, "c d"]`` inline lists
* block lists (``key:`` followed by ``- item`` lines)
* one level of nested mapping (``metadata:`` followed by indented ``k: v``)

Unknown or malformed lines are collected in ``warnings`` and never fatal
(07 §2.2: "Unknown keys are logged, not fatal").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frontmatter:
    data: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    warnings: list[str] = field(default_factory=list)
    present: bool = False


def _scalar(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    if (s[0] == s[-1]) and s[0] in "\"'" and len(s) >= 2:
        inner = s[1:-1]
        return inner.replace("\\n", "\n") if s[0] == '"' else inner
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _inline_list(raw: str) -> list[Any]:
    inner = raw.strip()[1:-1]
    items: list[str] = []
    buf, quote = "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf)
    return [_scalar(i) for i in items if i.strip()]


def _value(raw: str) -> Any:
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        return _inline_list(s)
    return _scalar(s)


def parse_frontmatter(text: str) -> Frontmatter:
    """Split ``---`` frontmatter from the body and parse it."""
    fm = Frontmatter(body=text)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        fm.warnings.append("unterminated frontmatter")
        return fm
    fm.present = True
    fm.body = "\n".join(lines[end + 1 :]).lstrip("\n")
    data: dict[str, Any] = {}
    current_key: str | None = None
    for lineno, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        stripped = line.strip()
        if indented and current_key is not None:
            container = data.get(current_key)
            if stripped.startswith("- "):
                if not isinstance(container, list):
                    container = [] if container in ("", None) else [container]
                    data[current_key] = container
                container.append(_value(stripped[2:]))
                continue
            if ":" in stripped:
                if not isinstance(container, dict):
                    container = {}
                    data[current_key] = container
                k, _, v = stripped.partition(":")
                container[k.strip()] = _value(v)
                continue
            fm.warnings.append(f"line {lineno}: unrecognised indented line")
            continue
        if ":" not in stripped:
            fm.warnings.append(f"line {lineno}: expected 'key: value'")
            continue
        k, _, v = stripped.partition(":")
        current_key = k.strip()
        data[current_key] = _value(v) if v.strip() else ""
    fm.data = data
    return fm


def as_list(value: Any) -> list[str]:
    """Normalise ``"a, b"`` / ``["a", "b"]`` / ``None`` to a list of strings
    (e.g. ``allowed-tools`` may be a string or a list, 07 §2.2)."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [p.strip() for p in str(value).split(",") if p.strip()]
