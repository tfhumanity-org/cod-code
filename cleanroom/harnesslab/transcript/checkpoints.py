"""File checkpoints (copy-on-first-touch) and rewind (WP4).

Implements spec 05 §2.5, R46 (on by default; ``CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING``;
SDK opt-in ``CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING``), R47 (only harness
edit tools tracked), R48 (rewind options incl. dry-run diffstat) and §4.9:
``before_edit`` backs up a file the first time it is touched
(``<hex16(sha256(path))>@v1``, ``None`` = did not exist); ``snapshot(message_id)``
per user prompt with stat-based dedup (mode+size+mtime, then content); cap 100
snapshots evicting unreferenced backups; ``rewind`` with link safety (skip
symlinks, hardlinks, non-regular files, changed real parent dir). Takeaway
05#T10. Backups live in ``ctx.file_history_dir``.

Lifecycle (05#T10)
------------------
1. Each user prompt: ``snapshot(message_id)`` records, for every tracked file,
   a backup of its *current* content (re-using the previous backup when the file
   is unchanged).
2. During the turn an edit tool calls ``before_edit(path)``. The first touch
   copies the untouched file to ``...@v1`` and - design choice - also adds that
   backup to the latest snapshot, because the untouched content *is* the state
   at that prompt. Without this a file first edited in turn N could not be
   restored by rewinding to turn N.
3. ``rewind(snapshot)`` restores every tracked file. A file not present in the
   chosen snapshot (first touched later) is restored from the earliest later
   snapshot that knows it, whose backup is again its pre-touch content.

Only harness edits are tracked (05 R47); shell edits are invisible here, which
the rewind reminder (``transcript.store.rewind_reminder``) tells the model.
"""

from __future__ import annotations

import difflib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harnesslab.config import env_truthy
from harnesslab.transcript.rows import FileBackup, FileSnapshot
from harnesslab.util.ids import hex16_path

if TYPE_CHECKING:
    from harnesslab.runtime import SessionContext

MAX_SNAPSHOTS = 100


@dataclass
class Diffstat:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    skipped: int = 0
    paths: list[str] = field(default_factory=list)


def checkpointing_enabled(ctx: "SessionContext") -> bool:
    """05 R46."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING"):
        return False
    if ctx.is_sdk and not env_truthy(ctx.env, "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"):
        return False
    return ctx.settings.get("fileCheckpointingEnabled", True) is not False


def _real_parent(path: str) -> str:
    return os.path.realpath(os.path.dirname(path))


def _read_lines(p: Path | None) -> list[str]:
    if p is None or not p.exists():
        return []
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


class FileHistory:
    def __init__(self, backup_dir: Path, *, enabled: bool = True) -> None:
        self.backup_dir = backup_dir
        self.enabled = enabled
        self.tracked: dict[str, FileBackup] = {}
        self.snapshots: list[FileSnapshot] = []
        self._max_version: dict[str, int] = {}
        self.transcript: Any = None  # optional TranscriptStore: snapshots logged as metadata rows

    @classmethod
    def for_session(cls, ctx: "SessionContext") -> "FileHistory":
        return cls(ctx.file_history_dir, enabled=checkpointing_enabled(ctx))

    # ------------------------------------------------------------ backups

    def _backup_path(self, name: str) -> Path:
        return self.backup_dir / name

    def _make_backup(self, path: str, version: int) -> FileBackup:
        """Copy ``path`` (mode preserved) to ``<hex16>@v<version>``; ``None`` name if absent."""
        name: str | None = None
        self._max_version[path] = max(self._max_version.get(path, 0), version)
        if os.path.isfile(path):
            name = f"{hex16_path(path)}@v{version}"
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, self._backup_path(name))
        return FileBackup(name, version, real_parent_dir=_real_parent(path))

    def _unchanged(self, path: str, prev: FileBackup) -> bool:
        """Stat comparison first (mode+size+mtime), then content (05 §4.9)."""
        exists = os.path.isfile(path)
        if prev.backup_file_name is None:
            return not exists
        bp = self._backup_path(prev.backup_file_name)
        if not exists or not bp.exists():
            return False
        a, b = os.stat(path), os.stat(bp)
        if a.st_size != b.st_size:
            return False
        if stat.S_IMODE(a.st_mode) == stat.S_IMODE(b.st_mode) and a.st_mtime_ns == b.st_mtime_ns:
            return True
        with open(path, "rb") as f1, open(bp, "rb") as f2:
            return f1.read() == f2.read()

    def before_edit(self, path: str) -> None:
        """Copy-on-first-touch (05 §4.9)."""
        if not self.enabled:
            return
        path = os.path.abspath(path)
        if path in self.tracked:
            return
        backup = self._make_backup(path, 1)
        self.tracked[path] = backup
        if self.snapshots:  # see module docstring, step 2
            self.snapshots[-1].tracked_file_backups.setdefault(path, backup)

    def snapshot(self, message_id: str) -> FileSnapshot:
        snap: dict[str, FileBackup] = {}
        if self.enabled:
            for path, prev in self.tracked.items():
                if self._unchanged(path, prev):
                    snap[path] = prev
                else:
                    # versions only grow, even after a rewind, so a backup name is never reused
                    version = max(prev.version, self._max_version.get(path, 0)) + 1
                    snap[path] = self._make_backup(path, version)
                    self.tracked[path] = snap[path]
        s = FileSnapshot(message_id, snap)
        self.snapshots.append(s)
        if len(self.snapshots) > MAX_SNAPSHOTS:
            self._evict()
        if self.transcript is not None and hasattr(self.transcript, "append_metadata"):
            self.transcript.append_metadata("file-history-snapshot", s.to_dict())
        return s

    def _evict(self) -> None:
        """Drop the oldest snapshots and delete backups nothing references any more."""
        dropped = self.snapshots[: len(self.snapshots) - MAX_SNAPSHOTS]
        self.snapshots = self.snapshots[-MAX_SNAPSHOTS:]
        live = {b.backup_file_name for s in self.snapshots for b in s.tracked_file_backups.values()}
        live |= {b.backup_file_name for b in self.tracked.values()}
        # The backup directory belongs to this session alone, so any file in it that no
        # remaining snapshot (or the current tracking state) points at can go - this also
        # catches first-touch backups that were superseded before any snapshot used them.
        candidates = {b.backup_file_name for s in dropped for b in s.tracked_file_backups.values()}
        if self.backup_dir.is_dir():
            candidates |= {p.name for p in self.backup_dir.iterdir() if "@v" in p.name}
        for name in candidates:
            if name and name not in live:
                try:
                    self._backup_path(name).unlink()
                except FileNotFoundError:
                    pass

    def find(self, message_id: str) -> FileSnapshot | None:
        for s in reversed(self.snapshots):
            if s.message_id == message_id:
                return s
        return None

    def load_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        """Rebuild state on resume from ``file-history-snapshot`` metadata rows."""
        self.snapshots = [FileSnapshot.from_dict(d) for d in snapshots]
        for s in self.snapshots:
            for p, b in s.tracked_file_backups.items():
                if p not in self.tracked or b.version >= self.tracked[p].version:
                    self.tracked[p] = b
                self._max_version[p] = max(self._max_version.get(p, 0), b.version)

    # ------------------------------------------------------------ rewind

    def _target_backup(self, snapshot: FileSnapshot, path: str) -> FileBackup | None:
        if path in snapshot.tracked_file_backups:
            return snapshot.tracked_file_backups[path]
        try:
            idx = next(i for i, s in enumerate(self.snapshots) if s is snapshot)
        except StopIteration:
            return None
        for later in self.snapshots[idx + 1 :]:
            if path in later.tracked_file_backups:
                return later.tracked_file_backups[path]
        return None

    @staticmethod
    def _link_unsafe(path: str, b: FileBackup) -> bool:
        if os.path.islink(path):
            return True
        if os.path.exists(path):
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                return True
        return bool(b.real_parent_dir) and _real_parent(path) != b.real_parent_dir

    def rewind(self, snapshot: FileSnapshot, *, dry_run: bool = False) -> Diffstat:
        """Restore tracked files to ``snapshot`` (05 §4.9); ``dry_run`` only measures."""
        stats = Diffstat()
        paths = list(dict.fromkeys([*snapshot.tracked_file_backups, *self.tracked]))
        for path in paths:
            b = self._target_backup(snapshot, path)
            if b is None:
                continue
            if self._link_unsafe(path, b):
                stats.skipped += 1
                continue
            src = self._backup_path(b.backup_file_name) if b.backup_file_name else None
            if src is not None and not src.exists():
                stats.skipped += 1
                continue
            current, target = _read_lines(Path(path)), _read_lines(src)
            exists = os.path.exists(path)
            if (src is None and not exists) or (src is not None and exists and current == target and self._same_bytes(path, src)):
                continue
            ins = dels = 0
            for line in difflib.ndiff(current, target):
                if line.startswith("+ "):
                    ins += 1
                elif line.startswith("- "):
                    dels += 1
            stats.files_changed += 1
            stats.insertions += ins
            stats.deletions += dels
            stats.paths.append(path)
            if dry_run:
                continue
            if src is None:
                os.unlink(path)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(src, path)
            self.tracked[path] = b
        return stats

    @staticmethod
    def _same_bytes(a: str, b: Path) -> bool:
        with open(a, "rb") as f1, open(b, "rb") as f2:
            return f1.read() == f2.read()
