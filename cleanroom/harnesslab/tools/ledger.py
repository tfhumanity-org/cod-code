"""Read ledger with content-aware staleness (WP2).

Implements spec 03 §2.3 (LedgerEntry; LRU keyed by absolute path, caps 5000
entries / 25 MiB; verbatim content only <= 4096 bytes; subagent clone drops
seeded entries), 03 R17-R22 and 03 §4.2; takeaways 03#T3, 03#T4, 03#T5; P12.

* ``check_before_write``: Edit/Write on an existing file need a non-partial
  entry (new files / empty ``old_string`` exempt) (R17); newer mtime is not
  stale if a full read's hash still matches (R18); real change + unique match +
  auto-allowed path -> ``STALE_RECOVERED`` with a warning (R19); else fail.
* ``after_write``: update content/hash/mtime; ``not_in_model_context`` (R20).
* ``read_dedup``: same path + range + unchanged mtime -> "unchanged" stub, or
  pointer to the startup-injected copy for seeded entries (R21).
* ``after_shell``: formatter-like commands re-stat ledger paths and report up to
  5 changed files (R22).

Design choices (spec silent)
----------------------------
* Hash: ``zlib.crc32`` + length (architect gap G11).
* ``mtime`` is the modification time floored to whole **milliseconds**, which
  is fine-grained enough that a ``touch`` within the same second is noticed.
* The 25 MiB cap weighs each entry by the size of the file it describes
  (``LedgerEntry.size``), so a few huge files evict many small ones; eviction
  only means "Read again before editing", never data loss.
* The text passed to ``record_read`` / ``after_write`` is always the *full*
  decoded file text exactly as on disk (line endings untouched), so hashes from
  Read and Edit are comparable.
* Paths are normalised with ``os.path.abspath`` (not ``realpath``) so the key is
  the path the model used.
"""

from __future__ import annotations

import enum
import os
import re
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace

MAX_ENTRIES = 5000
MAX_TOTAL_BYTES = 25 * 1024 * 1024
VERBATIM_MAX_BYTES = 4096
FORMATTER_NOTE_MAX_FILES = 5

#: 03 §4.2 ``FORMATTER_PATTERN``: commands that may rewrite files the model has
#: read. Deliberately broad - a false positive only costs a few stat calls.
FORMATTER_PATTERN = re.compile(
    r"""
    --write\b | --fix\b | --in-place\b
    | (?:^|[\s;&|(])(?:
          black | isort | rustfmt | gofmt | goimports | prettier | autopep8 | yapf
        | clang-format | swiftformat | ktlint | rubocop | standardrb | shfmt | stylua
      )(?=$|[\s;&|)])
    | \bruff\s+(?:format|check\s+.*--fix)
    | \bcargo\s+(?:fmt|clippy\s+.*--fix)
    | \bgo\s+fmt\b
    | \b(?:terraform|mix|dart|deno|biome|zig)\s+fmt\b
    | \b(?:mix|dart|biome)\s+format\b
    | \bsed\s+(?:-[a-zA-Z]*i|--in-place)
    """,
    re.VERBOSE,
)


@dataclass
class LedgerEntry:
    content: str | None
    content_hash: str
    mtime: int
    offset: int | None = None
    limit: int | None = None
    partial_view: bool = False
    not_in_model_context: bool = False
    seeded_from_context: bool = False
    hit_limit: bool = False
    size: int = 0


class StalenessVerdict(str, enum.Enum):
    OK = "ok"
    OK_STALE_RECOVERED = "ok_stale_recovered"
    NOT_READ = "not_read"
    STALE = "stale"


def content_hash(text: str) -> str:
    """Fast non-cryptographic fingerprint: crc32 of the UTF-8 bytes plus length."""
    data = text.encode("utf-8", "surrogatepass")
    return f"{zlib.crc32(data):08x}:{len(data)}"


def floor_mtime(path: str) -> int:
    """Modification time floored to whole milliseconds (see design choices)."""
    return os.stat(path).st_mtime_ns // 1_000_000


def _safe_mtime(path: str) -> int | None:
    try:
        return floor_mtime(path)
    except OSError:
        return None


def _norm(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _read_disk_text(path: str) -> str | None:
    """Decode a file the same way the Read tool does (BOM-aware)."""
    try:
        from harnesslab.tools.builtin.read import read_text_file  # same WP; lazy to avoid a cycle

        return read_text_file(path).text
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def was_full_read(e: LedgerEntry) -> bool:
    """03 §4.2: only a full, untruncated view can vouch for the whole file's hash."""
    return e.offset in (None, 1) and not e.partial_view and not e.hit_limit


class ReadLedger:
    """LRU map ``abs path -> LedgerEntry`` plus the staleness policies of 03 §4.2.

    Thread-safe: concurrency-safe Reads record entries from worker threads.
    """

    def __init__(self, *, max_entries: int = MAX_ENTRIES, max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self._entries: OrderedDict[str, LedgerEntry] = OrderedDict()
        self._total = 0
        self._lock = threading.RLock()
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes

    # ------------------------------------------------------------- LRU basics
    def get(self, path: str) -> LedgerEntry | None:
        key = _norm(path)
        with self._lock:
            e = self._entries.get(key)
            if e is not None:
                self._entries.move_to_end(key)
            return e

    def put(self, path: str, entry: LedgerEntry) -> None:
        """Insert/refresh with LRU eviction by count and total bytes."""
        key = _norm(path)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total -= old.size
            self._entries[key] = entry
            self._total += entry.size
            # Evict least-recently-used entries, but never the one just inserted.
            while len(self._entries) > 1 and (
                len(self._entries) > self.max_entries or self._total > self.max_total_bytes
            ):
                _, victim = self._entries.popitem(last=False)
                self._total -= victim.size

    def items(self) -> list[tuple[str, LedgerEntry]]:
        with self._lock:
            return list(self._entries.items())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        return self._total

    def snapshot(self) -> list[tuple[str, LedgerEntry]]:
        """Most-recent-first copy; used by compaction re-injection (05 §4.5)."""
        with self._lock:
            return [(p, replace(e)) for p, e in reversed(self._entries.items())]

    def clone_for_subagent(self) -> "ReadLedger":
        """03 §2.3: a subagent inherits what the parent read, minus startup-seeded
        entries (it did not receive the startup context those pointed at)."""
        c = ReadLedger(max_entries=self.max_entries, max_total_bytes=self.max_total_bytes)
        for p, e in self.items():
            if not e.seeded_from_context:
                c.put(p, replace(e))
        return c

    # ------------------------------------------------------------- policies
    def record_read(
        self,
        path: str,
        text: str,
        *,
        offset: int | None,
        limit: int | None,
        partial: bool,
        hit_limit: bool = False,
        seeded: bool = False,
    ) -> None:
        """Record what the model was shown. ``text`` is the full file text."""
        nbytes = len(text.encode("utf-8", "surrogatepass"))
        self.put(
            path,
            LedgerEntry(
                content=text if nbytes <= VERBATIM_MAX_BYTES else None,
                content_hash=content_hash(text),
                mtime=_safe_mtime(_norm(path)) or 0,
                offset=offset,
                limit=limit,
                partial_view=partial,
                seeded_from_context=seeded,
                hit_limit=hit_limit,
                size=nbytes,
            ),
        )

    def check_before_write(
        self, path: str, old_string: str | None, *, is_new_file: bool, auto_allowed: bool
    ) -> StalenessVerdict:
        """03 §4.2 ``check_before_write`` (R17-R19)."""
        if is_new_file or old_string == "":
            return StalenessVerdict.OK
        key = _norm(path)
        e = self.get(key)
        if e is None or e.partial_view:
            return StalenessVerdict.NOT_READ
        disk_mtime = _safe_mtime(key)
        if disk_mtime is None or disk_mtime <= e.mtime:
            return StalenessVerdict.OK
        current = _read_disk_text(key)
        if current is None:
            return StalenessVerdict.STALE
        if was_full_read(e) and content_hash(current) == e.content_hash:
            # Touched, or a formatter wrote identical bytes (R18): refresh the mtime
            # so the next check is cheap.
            with self._lock:
                e.mtime = disk_mtime
            return StalenessVerdict.OK
        if old_string and auto_allowed and current.count(old_string) == 1:
            return StalenessVerdict.OK_STALE_RECOVERED
        return StalenessVerdict.STALE

    def after_write(self, path: str, new_content: str, *, model_saw_full: bool) -> None:
        """R20: the harness wrote ``new_content``; the model's view is now current
        (or flagged ``not_in_model_context`` if it never saw the whole text)."""
        nbytes = len(new_content.encode("utf-8", "surrogatepass"))
        self.put(
            path,
            LedgerEntry(
                content=new_content if nbytes <= VERBATIM_MAX_BYTES else None,
                content_hash=content_hash(new_content),
                mtime=_safe_mtime(_norm(path)) or 0,
                not_in_model_context=not model_saw_full,
                size=nbytes,
            ),
        )

    def read_dedup(self, path: str, offset: int | None, limit: int | None) -> str | None:
        """``None`` | ``"unchanged"`` | ``"already_in_context"`` (R21).

        Entries the harness wrote without the model seeing the full text
        (``not_in_model_context``) never dedup: re-reading them is legitimate.
        """
        key = _norm(path)
        e = self.get(key)
        if e is None or e.not_in_model_context:
            return None
        if e.offset != offset or e.limit != limit:
            return None
        if _safe_mtime(key) != e.mtime:
            return None
        return "already_in_context" if e.seeded_from_context else "unchanged"

    def after_shell(self, command: str) -> str | None:
        """Note text naming changed files, or None (R22, 03#T4)."""
        if not FORMATTER_PATTERN.search(command):
            return None
        changed: list[str] = []
        for p, e in self.items():
            m = _safe_mtime(p)
            if m is not None and m > e.mtime:
                changed.append(p)
        if not changed:
            return None
        shown = ", ".join(changed[:FORMATTER_NOTE_MAX_FILES])
        more = len(changed) - FORMATTER_NOTE_MAX_FILES
        tail = f" (and {more} more)" if more > 0 else ""
        return (
            f"Note: this command modified {len(changed)} file(s) you read earlier: {shown}{tail}. "
            "Read them again before editing them."
        )


def verdict_message(verdict: StalenessVerdict, path: str) -> str:
    """Model-facing text for a failed or recovered staleness check (03#T11:
    every error names the recovery step)."""
    if verdict is StalenessVerdict.NOT_READ:
        return (
            f"{path} has not been read in full yet. Use the Read tool on it (without offset/limit) "
            "before writing to it."
        )
    if verdict is StalenessVerdict.STALE:
        return (
            f"{path} was modified after you last read it (by the user, a formatter or another "
            "process). Read it again, then retry the change."
        )
    if verdict is StalenessVerdict.OK_STALE_RECOVERED:
        return (
            f"Warning: {path} had changed on disk since your last Read. The edit was still applied "
            "because the text to replace matched exactly once, but the file now contains changes "
            "you have not seen. Read it before making further assumptions about its contents."
        )
    return ""
