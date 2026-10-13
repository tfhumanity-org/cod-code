"""Read tool (WP2).

Implements spec 03 §2.2 (Read row), R21 (dedup stub), R48 (2000 lines,
``N<TAB>line``, ``:`` separator when lines start with a tab, 256 KiB / 25 000
tokens with bytes/128 pre-estimate), R49 + §4.6 (auto-pagination: x0.85 then
x0.7 for <= 6 iterations, ``partial_view`` banner with next offset; explicit
over-cap range is an error), R50 (binary extensions refused, /dev/stdin and
FIFOs refused, "did you mean", empty file / offset past EOF -> reminder), R51
(images by magic bytes; stdlib cannot resize - design choice: return as-is if
under 5 MiB base64 else refuse with a hint; token estimate ceil(w/28)*ceil(h/28)
when dimensions are parseable), R52-R53 (PDF/notebook: notebook cells
supported; PDF returns a not-supported note - spec gap for stdlib).
Records every read in the ledger (03 §2.3) and ``ctx.touched_paths`` (02 R20).

Design choices (spec silent)
----------------------------
* The ``:`` separator is chosen per line: a line whose text starts with a tab
  is rendered ``N:<tab>...`` so the number/text boundary stays unambiguous.
* A whole-file read of a file over 256 KiB is refused with guidance to use
  ``offset``/``limit`` or Grep (the byte cap applies before pagination).
* A default read that stops at 2000 lines appends a short continuation note and
  records ``hit_limit`` (the model has not seen the whole file).
* ``partial_view`` is set only for an *auto-paginated* whole-file read (the
  model was told it saw an incomplete first page). An explicit offset/limit
  read is recorded with its range, which already disables the hash-based
  staleness recovery (``ledger.was_full_read``), but does not block editing -
  otherwise files too large for one read could never be edited.
* Dedup kill switch: ``CLAUDE_CODE_DISABLE_READ_DEDUP``.
* Images larger than 2000 px are passed through unchanged (no resizing in the
  stdlib) with a note; only the 5 MiB base64 cap is enforced.
"""

from __future__ import annotations

import base64
import difflib
import json
import math
import os
import stat
import struct
from dataclasses import dataclass
from typing import Any

from harnesslab.config import env_int, env_truthy
from harnesslab.tokens import estimate_text_tokens
from harnesslab.tools.base import UNBOUNDED, Tool, ToolCallContext, ToolErrorCode, ToolOutput, ValidationResult
from harnesslab.types import ImageBlock, TextBlock
from harnesslab.util.text import wrap_system_reminder

DEFAULT_LINES = 2000
MAX_BYTES = 256 * 1024
MAX_TOKENS = 25_000
BINARY_EXTENSIONS = frozenset({".zip", ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".o", ".a", ".tar", ".gz", ".7z", ".mp3", ".mp4", ".mov", ".wav"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
MAX_IMAGE_B64 = 5 * 1024 * 1024
MAX_IMAGE_EDGE = 2000
PAGINATE_FIRST, PAGINATE_SHRINK, PAGINATE_ITERS = 0.85, 0.7, 6


# ------------------------------------------------------------ file decoding


@dataclass
class TextFile:
    text: str  # decoded, line endings as on disk
    encoding: str  # "utf-8" | "utf-16-le"
    bom: bool
    eol: str  # "\n" | "\r\n"


def read_text_file(path: str) -> TextFile:
    """Decode a text file, detecting BOM / UTF-16LE / CRLF (03 R33).
    Raises ``UnicodeDecodeError`` for content that is not text."""
    with open(path, "rb") as f:
        raw = f.read()
    return decode_text(raw)


def decode_text(raw: bytes) -> TextFile:
    if raw.startswith(b"\xef\xbb\xbf"):
        text, enc, bom = raw[3:].decode("utf-8"), "utf-8", True
    elif raw.startswith(b"\xff\xfe"):
        text, enc, bom = raw[2:].decode("utf-16-le"), "utf-16-le", True
    else:
        if b"\x00" in raw[:8192]:
            raise UnicodeDecodeError("utf-8", raw[:1], 0, 1, "NUL byte: binary content")
        text, enc, bom = raw.decode("utf-8"), "utf-8", False
    eol = "\r\n" if "\r\n" in text else "\n"
    return TextFile(text, enc, bom, eol)


def encode_text(text: str, encoding: str, bom: bool) -> bytes:
    data = text.encode(encoding)
    if bom:
        data = (b"\xef\xbb\xbf" if encoding == "utf-8" else b"\xff\xfe") + data
    return data


# ----------------------------------------------------------------- images


def sniff_image(head: bytes) -> str | None:
    """Media type from magic bytes (R51)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_size(data: bytes, media_type: str) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` without third-party libraries."""
    try:
        if media_type == "image/png":
            return struct.unpack(">II", data[16:24])
        if media_type == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if media_type == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if media_type == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
                i += 2 + seg
    except (struct.error, IndexError):
        return None
    return None


def image_tokens(w: int, h: int) -> int:
    """R51 token estimate: 28-pixel tiles."""
    return math.ceil(w / 28) * math.ceil(h / 28)


# ---------------------------------------------------------------- numbering


def format_numbered(lines: list[str], start: int) -> str:
    """R48: ``N<TAB>line``; ``N:line`` for lines that start with a tab."""
    out = []
    for i, line in enumerate(lines, start=start):
        sep = ":" if line.startswith("\t") else "\t"
        out.append(f"{i}{sep}{line}")
    return "\n".join(out)


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _over_budget(body: str, max_tokens: int) -> bool:
    # cheap pre-check first (bytes/128), then the real estimate (03 §4.6)
    if len(body.encode("utf-8", "surrogatepass")) / 128 > max_tokens:
        return True
    return estimate_text_tokens(body) > max_tokens


def did_you_mean(path: str) -> str | None:
    parent, name = os.path.split(path)
    try:
        names = os.listdir(parent or ".")
    except OSError:
        return None
    hits = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return os.path.join(parent, hits[0]) if hits else None


def render_notebook(raw: str) -> str:
    """R53: one block per cell, tagged with its id, outputs included."""
    nb = json.loads(raw)
    parts = []
    for idx, cell in enumerate(nb.get("cells", [])):
        cid = cell.get("id") or f"cell-{idx}"
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else str(src)
        block = [f'<cell id="{cid}" type="{cell.get("cell_type", "code")}">', src]
        for out in cell.get("outputs", []) or []:
            text = out.get("text")
            if text is None:
                text = (out.get("data") or {}).get("text/plain")
            if text is None and out.get("ename"):
                text = f"{out.get('ename')}: {out.get('evalue', '')}"
            if text is not None:
                block.append("<output>" + ("".join(text) if isinstance(text, list) else str(text)) + "</output>")
        block.append("</cell>")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


# --------------------------------------------------------------------- tool


class ReadTool(Tool):
    name = "Read"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
            "pages": {"type": "string"},
        },
        "required": ["file_path"],
        "additionalProperties": False,
    }
    max_result_chars = UNBOUNDED
    rule_content_field = "file_path"
    kind = "read"
    search_hint = "read file contents"

    def description(self, ctx: Any = None) -> str:
        return "Read a file from the local filesystem (text, images, notebooks)."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        lean_text = (
            "Reads a file by absolute path. Output lines are numbered starting at 1 (`N<TAB>text`). "
            f"By default up to {DEFAULT_LINES} lines are returned; use offset/limit for large files. "
            "Images are returned visually; .ipynb notebooks as cells with outputs. Don't re-read a file "
            "you just edited - the edit result already tells you its state. Read several files in "
            "parallel when you need them."
        )
        if lean:
            return lean_text
        return (
            lean_text
            + "\n\nExamples:\n- Read the whole file: {\"file_path\": \"/repo/src/app.py\"}\n"
            "- Continue a long file: {\"file_path\": \"/repo/log.txt\", \"offset\": 2001, \"limit\": 500}\n"
            "The line-number prefix is not part of the file: never include it in Edit's old_string."
        )

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def classifier_projection(self, input: dict[str, Any]) -> str:
        return str(input.get("file_path", ""))

    # -- validation (R50) ----------------------------------------------------
    def validate_input(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ValidationResult:
        path = input["file_path"]
        if not os.path.isabs(path):
            return ValidationResult.fail(
                f"file_path must be an absolute path, got {path!r}. The current directory is {call_ctx.session.cwd}.",
                ToolErrorCode.INPUT_VALIDATION,
            )
        if path in ("/dev/stdin", "/dev/tty") or path.startswith(("/dev/fd/", "/proc/self/fd/")):
            return ValidationResult.fail(
                f"Reading {path} is not supported: it would block waiting for input.", ToolErrorCode.INPUT_VALIDATION
            )
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return ValidationResult.fail(
                f"{path} is a binary file ({ext}) and cannot be read as text. Use a skill or a shell "
                "tool suited to this format to inspect it.",
                ToolErrorCode.BINARY_FILE,
            )
        if not os.path.exists(path):
            msg = f"File does not exist: {path}. Current working directory: {call_ctx.session.cwd}."
            alt = did_you_mean(path)
            if alt:
                msg += f" Did you mean {alt}?"
            return ValidationResult.fail(msg, ToolErrorCode.FILE_NOT_FOUND)
        try:
            st = os.stat(path)
        except OSError as e:
            return ValidationResult.fail(f"Cannot access {path}: {e}", ToolErrorCode.FILE_NOT_FOUND)
        if stat.S_ISFIFO(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISSOCK(st.st_mode):
            return ValidationResult.fail(
                f"{path} is a pipe or device, not a regular file; reading it could block.", ToolErrorCode.INPUT_VALIDATION
            )
        if stat.S_ISDIR(st.st_mode):
            return ValidationResult.fail(
                f"{path} is a directory. Use Glob or `ls` via Bash to list it.", ToolErrorCode.INPUT_VALIDATION
            )
        return ValidationResult()

    # -- call ----------------------------------------------------------------
    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        path = os.path.abspath(input["file_path"])
        offset, limit = input.get("offset"), input.get("limit")
        if path not in ctx.touched_paths:
            ctx.touched_paths.append(path)

        ledger = ctx.ledger
        if ledger is not None and not env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_READ_DEDUP"):
            stub = ledger.read_dedup(path, offset, limit)
            if stub == "unchanged":
                return ToolOutput(
                    data=(
                        "File unchanged since you last read it with the same offset/limit. Use the "
                        "earlier Read result in this conversation instead of reading it again."
                    )
                )
            if stub == "already_in_context":
                return ToolOutput(
                    data=(
                        "This file's contents were already provided in context at the start of the "
                        "session and it has not changed since. Refer to that copy."
                    )
                )

        with open(path, "rb") as f:
            head = f.read(32)
        media = sniff_image(head)
        ext = os.path.splitext(path)[1].lower()
        if media:
            return self._read_image(path, media)
        if ext in IMAGE_EXTENSIONS:
            return ToolOutput(
                data=(
                    f"{path} is named like an image, yet its bytes do not decode as any known image format. If it "
                    "was downloaded, the download probably saved an error or login page instead."
                ),
                is_error=True,
            )
        if ext == ".pdf" or head.startswith(b"%PDF"):
            return ToolOutput(
                data=(
                    "PDF reading is not supported in this harness (no PDF renderer available). Extract "
                    "the text with a shell tool such as `pdftotext` if it is installed."
                ),
                is_error=True,
            )
        if ext == ".ipynb":
            tf = read_text_file(path)
            body = render_notebook(tf.text)
            if ledger is not None:
                ledger.record_read(path, tf.text, offset=None, limit=None, partial=False)
            return ToolOutput(data=body)
        return self._read_text(path, offset, limit, call_ctx)

    def _read_image(self, path: str, media: str) -> ToolOutput:
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        if len(b64) > MAX_IMAGE_B64:
            return ToolOutput(
                data=(
                    f"Image {path} is too large to attach ({len(b64)} bytes as base64, limit "
                    f"{MAX_IMAGE_B64}). Downscale it first (e.g. `sips -Z 2000` or ImageMagick) and Read the result."
                ),
                is_error=True,
            )
        dims = image_size(data, media)
        note = f"Image {path} ({media}"
        if dims:
            w, h = dims
            note += f", {w}x{h} px, about {image_tokens(w, h)} tokens"
            if max(w, h) > MAX_IMAGE_EDGE:
                note += "; larger than 2000 px and shown at original size"
        note += ")."
        return ToolOutput(data={"type": "image", "path": path, "media_type": media}, blocks=[ImageBlock(media, b64), TextBlock(note)])

    def _read_text(self, path: str, offset: int | None, limit: int | None, call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        explicit = offset is not None or limit is not None
        size = os.path.getsize(path)
        if size > MAX_BYTES and not explicit:
            return ToolOutput(
                data=(
                    f"{path} is {size} bytes, over the {MAX_BYTES // 1024} KiB limit for reading a whole "
                    "file. Read a range with offset and limit, or use Grep to find the relevant part."
                ),
                is_error=True,
            )
        try:
            tf = read_text_file(path)
        except UnicodeDecodeError:
            return ToolOutput(
                data=f"{path} does not look like a text file (it is not valid UTF-8/UTF-16). It cannot be read as text.",
                is_error=True,
            )
        lines = split_lines(tf.text)
        total = len(lines)
        ledger = ctx.ledger
        if total == 0:
            if ledger is not None:
                ledger.record_read(path, tf.text, offset=offset, limit=limit, partial=False)
            return ToolOutput(data=wrap_system_reminder(f"Warning: {path} exists but is empty."))
        start = offset or 1
        if start > total:
            return ToolOutput(
                data=wrap_system_reminder(
                    f"Warning: {path} has only {total} lines, so offset {start} is past the end of the file."
                )
            )
        count = limit or DEFAULT_LINES
        sel = lines[start - 1 : start - 1 + count]
        body = format_numbered(sel, start)
        max_tokens = env_int(ctx.env, "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", MAX_TOKENS, lo=1)
        partial = False
        hit_limit = not explicit and len(sel) < total
        note = ""
        if _over_budget(body, max_tokens):
            if explicit:
                return ToolOutput(
                    data=(
                        f"The requested range is about {estimate_text_tokens(body)} tokens, over the "
                        f"{max_tokens}-token limit for one read. Use a smaller limit."
                    ),
                    is_error=True,
                )
            # R49 / §4.6 auto-pagination
            keep = max(1, int(len(sel) * max_tokens / max(1, estimate_text_tokens(body)) * PAGINATE_FIRST))
            for _ in range(PAGINATE_ITERS):
                body = format_numbered(sel[:keep], start)
                if not _over_budget(body, max_tokens):
                    break
                keep = max(1, int(keep * PAGINATE_SHRINK))
            partial, hit_limit = True, False
            note = "\n\n" + wrap_system_reminder(
                f"Partial view: showing lines 1-{keep} of {total}. Because the whole file exceeds what one read "
                f"can return, keep going with offset={keep + 1}. Do not answer questions about the whole file from this "
                "first page alone."
            )
        elif hit_limit:
            note = "\n\n" + wrap_system_reminder(
                f"Showing lines 1-{len(sel)} of {total}. Continue with offset={len(sel) + 1} to read more."
            )
        if ledger is not None:
            ledger.record_read(path, tf.text, offset=offset, limit=limit, partial=partial, hit_limit=hit_limit)
        return ToolOutput(data=body + note)
