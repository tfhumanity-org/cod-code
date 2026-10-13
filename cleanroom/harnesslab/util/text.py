"""Small text helpers shared by all modules (FINAL - architect).

* ``wrap_system_reminder`` - 02 R30: every meta text block and all hook output is
  wrapped in ``<system-reminder>`` tags.
* ``escape_reminder_close`` - 03 R65: injected notifications must escape closing
  reminder tags in their content so they cannot break out of the envelope.
* ``head`` - bounded excerpts with an explicit cut note (02 R24, R28; P09 "say
  what was cut").
* ``human_size`` - sizes in spill notices (03 R24).
"""

from __future__ import annotations

REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"


def escape_reminder_close(text: str) -> str:
    """Neutralise any closing reminder tag inside untrusted text (03 R65)."""
    return text.replace(REMINDER_CLOSE, "<\\/system-reminder>")


def wrap_system_reminder(text: str) -> str:
    """Wrap ``text`` in reminder tags, escaping embedded closing tags first."""
    return f"{REMINDER_OPEN}\n{escape_reminder_close(text)}\n{REMINDER_CLOSE}"


def head(text: str, *, max_bytes: int | None = None, max_lines: int | None = None) -> tuple[str, bool]:
    """Return ``(excerpt, truncated)`` keeping whole lines within both limits.

    Whichever limit bites first wins (02 R24: "200 lines or 25 000 bytes,
    whichever comes first").
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    used = 0
    truncated = False
    for i, line in enumerate(lines):
        if max_lines is not None and i >= max_lines:
            truncated = True
            break
        n = len(line.encode("utf-8"))
        if max_bytes is not None and used + n > max_bytes:
            truncated = True
            break
        out.append(line)
        used += n
    return "".join(out), truncated


def human_size(n: int) -> str:
    """``1536 -> '1.5 KB'``."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover
