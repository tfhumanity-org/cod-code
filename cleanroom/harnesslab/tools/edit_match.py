"""Forgiving string matching for Edit, with style restoration (WP2).

Implements spec 03 R27-R33 and 03 §4.3; takeaway 03#T6.

Cascade in ``find_actual``: exact -> curly-quote folding (return the file's real
substring) -> decoded unicode escapes in ``old`` -> escaped-regex search when
the file contains ``\\u`` escapes (respecting backslash parity). If still not
found, retry after reversing API-side tag sanitisation (``SANITIZE_MAP``, own
choice of placeholder set) applied to both old and new (R29).

``restyle_new`` re-expresses ``new`` in the file's own style: re-curl quotes
(opening after whitespace/([{/dash, closing otherwise; apostrophe between
letters) and re-escape non-ASCII with the file's hex case.

``apply_edit_text`` also: strips trailing whitespace per line of ``new`` for
non-Markdown (R28); deletes the trailing newline when ``new == ""`` (R30);
errors with the count and code 9 on multiple matches (R31); multi-edit
sequential rules (R32). Encoding/BOM/EOL preservation happens in the Edit tool
(R33).

All functions here are pure (string in, string out) so they are easy to test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harnesslab.tools.base import ToolErrorCode

#: placeholder tag -> real tag (03 R29). Own choice of entries: short tags an
#: API layer might substitute for markup that looks like conversation structure.
SANITIZE_MAP: dict[str, str] = {
    "<fnr>": "<function_results>",
    "</fnr>": "</function_results>",
    "<n>": "<name>",
    "</n>": "</name>",
    "<o>": "<output>",
    "</o>": "</output>",
    "<e>": "<error>",
    "</e>": "</error>",
    "<s>": "<system>",
    "</s>": "</system>",
    "<r>": "<result>",
    "</r>": "</result>",
    "\n\nH:": "\n\nHuman:",
    "\n\nA:": "\n\nAssistant:",
}

LEFT_SINGLE, RIGHT_SINGLE = "‘", "’"
LEFT_DOUBLE, RIGHT_DOUBLE = "“", "”"
_SINGLE_CURLY = "‘’‚‛"
_DOUBLE_CURLY = "“”„‟"
_FOLD = str.maketrans({**{c: "'" for c in _SINGLE_CURLY}, **{c: '"' for c in _DOUBLE_CURLY}})

_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
#: characters after which a quote is an *opening* quote
_OPENERS = set(" \t\n\r([{<—–-")


@dataclass
class EditOutcome:
    ok: bool
    content: str = ""
    message: str = ""
    error_code: ToolErrorCode = ToolErrorCode.NONE
    replacements: int = 0
    used_quote_folding: bool = False
    used_escape_matching: bool = False


# ----------------------------------------------------------------- helpers


def fold_quotes(s: str) -> str:
    """Curly quotes -> straight quotes. Length-preserving, so indices carry over."""
    return s.translate(_FOLD)


def has_unicode_escapes(s: str) -> bool:
    return bool(_ESCAPE_RE.search(s))


def decode_escapes(s: str) -> str:
    """``\\u00e9`` -> ``é`` (surrogate pairs joined)."""
    out = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    try:
        return out.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return out


def _escape_char(ch: str, upper: bool) -> str:
    def u(cp: int) -> str:
        h = f"{cp:04x}"
        return "\\u" + (h.upper() if upper else h)

    cp = ord(ch)
    if cp <= 0xFFFF:
        return u(cp)
    cp -= 0x10000
    return u(0xD800 + (cp >> 10)) + u(0xDC00 + (cp & 0x3FF))


def escape_non_ascii(s: str, *, upper: bool = False) -> str:
    return "".join(ch if ord(ch) < 128 else _escape_char(ch, upper) for ch in s)


def _escaped_regex_search(file: str, old: str) -> str | None:
    """Find ``old`` in a file that spells non-ASCII characters as ``\\uXXXX``.

    Each non-ASCII char of ``old`` may appear literally or as an escape. An
    escape only counts if its backslash is not itself escaped, i.e. it is
    preceded by an even number of backslashes ("backslash parity").
    """
    parts: list[str] = []
    for ch in old:
        if ord(ch) < 128:
            parts.append(re.escape(ch))
        else:
            esc = _escape_char(ch, upper=False)
            alt = "".join(
                "\\\\u" + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in piece)
                for piece in esc.split("\\u")[1:]
            )
            parts.append(f"({re.escape(ch)}|{alt})")
    try:
        rx = re.compile("".join(parts))
    except re.error:
        return None
    for m in rx.finditer(file):
        ok = True
        for g in range(1, (rx.groups or 0) + 1):
            if m.group(g) and m.group(g).startswith("\\"):
                i, n = m.start(g) - 1, 0
                while i >= 0 and file[i] == "\\":
                    n, i = n + 1, i - 1
                if n % 2:
                    ok = False
                    break
        if ok:
            return m.group(0)
    return None


def find_actual(file: str, old: str) -> str | None:
    """03 §4.3 cascade. Returns the substring *as it appears in the file*."""
    if not old:
        return None
    if old in file:
        return old
    i = fold_quotes(file).find(fold_quotes(old))
    if i >= 0:
        return file[i : i + len(old)]
    if has_unicode_escapes(old):
        dec = decode_escapes(old)
        if dec in file:
            return dec
    if any(ord(c) > 127 for c in old) and "\\u" in file:
        return _escaped_regex_search(file, old)
    return None


def _recurl(new: str, *, doubles: bool, singles: bool) -> str:
    out: list[str] = []
    for i, ch in enumerate(new):
        prev = new[i - 1] if i > 0 else ""
        nxt = new[i + 1] if i + 1 < len(new) else ""
        opening = prev == "" or prev in _OPENERS
        if ch == '"' and doubles:
            out.append(LEFT_DOUBLE if opening else RIGHT_DOUBLE)
        elif ch == "'" and singles:
            if prev.isalpha() and nxt.isalpha():
                out.append(RIGHT_SINGLE)  # apostrophe: don't, it's
            else:
                out.append(LEFT_SINGLE if opening else RIGHT_SINGLE)
        else:
            out.append(ch)
    return "".join(out)


def restyle_new(new: str, actual_old: str, original_old: str) -> str:
    """Re-express ``new`` in the file's own quoting / escaping style (R27)."""
    if actual_old == original_old:
        return new
    doubles = any(c in actual_old for c in _DOUBLE_CURLY) and not any(c in original_old for c in _DOUBLE_CURLY)
    singles = any(c in actual_old for c in _SINGLE_CURLY) and not any(c in original_old for c in _SINGLE_CURLY)
    if doubles or singles:
        new = _recurl(new, doubles=doubles, singles=singles)
    if "\\u" in actual_old and "\\u" not in original_old:
        # matched via escapes: the file spells non-ASCII as \uXXXX - do the same
        hexes = re.findall(r"\\u([0-9a-fA-F]{4})", actual_old)
        upper = any(any(c in "ABCDEF" for c in h) for h in hexes)
        new = escape_non_ascii(new, upper=upper)
    elif has_unicode_escapes(original_old) and not has_unicode_escapes(actual_old):
        # the model wrote escapes but the file has real characters
        new = decode_escapes(new)
    return new


def desanitize(s: str) -> str:
    """Undo API-side tag sanitisation (R29)."""
    for placeholder, real in SANITIZE_MAP.items():
        s = s.replace(placeholder, real)
    return s


def strip_trailing_ws(s: str) -> str:
    """R28: strip spaces/tabs at the end of every line, keeping line endings."""
    return re.sub(r"[ \t]+(?=\r?\n|$)", "", s)


def _quote(s: str, limit: int = 500) -> str:
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit} more characters]"


# ------------------------------------------------------------------ edits


def apply_edit_text(
    content: str, old: str, new: str, *, replace_all: bool = False, is_markdown: bool = False
) -> EditOutcome:
    """Apply one edit to ``content`` (03 §4.3 ``apply_edit`` minus file I/O)."""
    if old == new:
        return EditOutcome(
            False,
            message="old_string and new_string are identical, so there is nothing to change.",
            error_code=ToolErrorCode.NO_CHANGE,
        )
    if old == "":
        if content.strip():
            return EditOutcome(
                False,
                message="old_string is empty but the file already has content. Provide the exact text to replace.",
                error_code=ToolErrorCode.INPUT_VALIDATION,
            )
        return EditOutcome(True, content=new, replacements=1)

    if not is_markdown:
        new = strip_trailing_ws(new)

    style_old = old
    actual = find_actual(content, old)
    if actual is None:
        old2, new2 = desanitize(old), desanitize(new)
        if (old2, new2) != (old, new):
            actual = find_actual(content, old2)
            if actual is not None:
                new, style_old = new2, old2
    if actual is None:
        tried_escapes = has_unicode_escapes(old) or any(ord(c) > 127 for c in old)
        note = " (matching with unicode escapes swapped was also tried)" if tried_escapes else ""
        return EditOutcome(
            False,
            message=f"The string to replace was not found in the file{note}.\nString: {_quote(old)}",
            error_code=ToolErrorCode.NOT_FOUND_IN_FILE,
        )

    n = content.count(actual)
    if n > 1 and not replace_all:
        return EditOutcome(
            False,
            message=(
                f"Found {n} matches of the string to replace, but replace_all is false. Set replace_all "
                "to true to change every occurrence, or include more surrounding context so the "
                f"string matches exactly once.\nString: {_quote(old)}"
            ),
            error_code=ToolErrorCode.MULTIPLE_MATCHES,
        )

    new = restyle_new(new, actual, style_old)
    if new == "" and not actual.endswith("\n") and (actual + "\n") in content:
        actual += "\n"  # R30: deleting a whole line removes its newline too
        n = content.count(actual)
    out = content.replace(actual, new) if replace_all else content.replace(actual, new, 1)
    if out == content:
        return EditOutcome(False, message="The edit produced no change.", error_code=ToolErrorCode.NO_CHANGE)
    folded = actual != style_old and fold_quotes(actual) == fold_quotes(style_old)
    return EditOutcome(
        True,
        content=out,
        replacements=n if replace_all else 1,
        used_quote_folding=folded,
        used_escape_matching=actual != style_old and not folded,
    )


def apply_multi_edit(
    content: str, edits: list[tuple[str, str, bool]], *, is_markdown: bool = False
) -> EditOutcome:
    """03 R32: sequential; reject if an old_string is contained in an earlier new_string,
    or if the final content equals the original."""
    current = content
    earlier_news: list[str] = []
    total = 0
    folding = escaping = False
    for i, (old, new, replace_all) in enumerate(edits, start=1):
        if old and any(old in prev for prev in earlier_news):
            return EditOutcome(
                False,
                message=(
                    f"Edit {i}: its old_string appears inside the new_string of an earlier edit, so "
                    "the edits would interfere. Combine them into one edit or reorder them."
                ),
                error_code=ToolErrorCode.INPUT_VALIDATION,
            )
        r = apply_edit_text(current, old, new, replace_all=replace_all, is_markdown=is_markdown)
        if not r.ok:
            return EditOutcome(False, message=f"Edit {i}: {r.message}", error_code=r.error_code)
        current = r.content
        total += r.replacements
        folding |= r.used_quote_folding
        escaping |= r.used_escape_matching
        earlier_news.append(new)
    if current == content:
        return EditOutcome(False, message="The edits leave the file unchanged.", error_code=ToolErrorCode.NO_CHANGE)
    return EditOutcome(True, content=current, replacements=total, used_quote_folding=folding, used_escape_matching=escaping)
