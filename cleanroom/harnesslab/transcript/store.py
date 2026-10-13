"""Append-only JSONL transcript with a uuid DAG: write, load, resume, fork, GC (WP4).

Implements spec 05 §2.3, §2.6, R36 (``parentUuid`` = previous non-progress row,
except tool_result rows parent to their issuing assistant row), R37
(last-wins metadata re-appended at the tail so a picker reads only the first
and last 64 KiB), R38 + §4.8 (GC: keep rows after the last boundary, the
preserved segment, dangling-parent targets and latest last-wins metadata;
>= 5 MiB; backstop 20 MiB doubling to 160 MiB when < 10 % saved; atomic
rewrite), R39 (persistence disabled by ``--no-session-persistence`` /
``CLAUDE_CODE_SKIP_PROMPT_HISTORY``), R40 + §4.7 (leaf selection: explicit
uuid > last-prompt leaf > newest non-sidechain row; cycle detector), R41
(picker filters, 200-char preview), R42 (session-id adoption vs fork), R43
(rewind = resume at uuid + fork), R44 (side chats: no transcript write);
takeaways 05#T9, 08#T7; P22.

Rows are produced/consumed with ``transcript.rows.item_to_row`` /
``row_to_item`` (FINAL).

How the DAG works (05#T9)
-------------------------
Every row names its parent. Walking parents from a *leaf* yields one linear
conversation; branches (forks, rewinds, parallel tool results) are just other
leaves. A compact boundary has ``parentUuid = None``, so the walk stops there
and a resumed session starts from the summary instead of the full history,
while ``logicalParentUuid`` still records where the old branch ended.

Design choices (spec silent)
----------------------------
* Metadata rows are ``{"type": t, "value": v, "sessionId", "timestamp"}``.
* The nested-session marker of R39 is the env var ``HARNESSLAB_NESTED_SESSION``.
* GC thresholds are constructor arguments so tests need not write 5 MiB.
* ``list_sessions`` hides rows whose first transcript row has ``isSidechain``,
  ``sessionKind`` in {team, daemon, loop}, an ``entrypoint`` starting with
  ``sdk``, or a ``superseded-by`` metadata row (05 R41).

Deferred: AI title generation (05 R45) - a host feature; titles are read from
``custom-title`` / ``ai-title`` rows here. Retention sweep (05 R49, architect
G16). Side-chat Q&A history (R44) is a host concern; ``link()`` gives side chats
the DAG without any disk write.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from harnesslab.config import env_truthy
from harnesslab.tokens import is_compact_boundary
from harnesslab.transcript.rows import FOLD_RULES, TRANSCRIPT_TYPES, item_to_row, row_to_item
from harnesslab.types import Attachment, ConversationItem, Message, ToolResultBlock, new_uuid, now_iso

if TYPE_CHECKING:
    from harnesslab.runtime import SessionContext

PICKER_WINDOW = 64 * 1024
GC_MIN_BYTES = 5 * 1024 * 1024
GC_BACKSTOP_START = 20 * 1024 * 1024
GC_BACKSTOP_MAX = 160 * 1024 * 1024
GC_MIN_SAVED_FRAC = 0.10
PREVIEW_CHARS = 200
HIDDEN_SESSION_KINDS = frozenset({"team", "daemon", "loop"})


@dataclass
class LoadedSession:
    session_id: str
    items: list[ConversationItem]
    metadata: dict[str, Any] = field(default_factory=dict)  # folded last-wins / accumulate values
    leaf_uuid: str | None = None
    forked: bool = False
    cycle_detected: bool = False


@dataclass
class SessionSummary:
    """One resume-picker entry (05 R41)."""

    session_id: str
    path: Path
    title: str | None
    first_prompt_preview: str
    modified: float


def persistence_enabled(env: Mapping[str, str], *, no_session_persistence: bool = False) -> bool:
    """05 R39: the flag, the env var, or a nested-session marker disable writes."""
    if no_session_persistence:
        return False
    return not (env_truthy(env, "CLAUDE_CODE_SKIP_PROMPT_HISTORY") or env_truthy(env, "HARNESSLAB_NESTED_SESSION"))


def _row_value(row: dict[str, Any]) -> Any:
    if "value" in row:
        return row["value"]
    return {k: v for k, v in row.items() if k not in ("type", "sessionId", "timestamp", "uuid")}


class TranscriptStore:
    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        cwd: str,
        enabled: bool = True,
        gc_min_bytes: int = GC_MIN_BYTES,
        gc_backstop: int = GC_BACKSTOP_START,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.session_id = session_id
        self.cwd = cwd
        self.enabled = enabled
        self.last_uuid: str | None = None
        self.gc_min_bytes = gc_min_bytes
        self.gc_backstop = gc_backstop
        self.extra_fields = dict(extra_fields or {})  # e.g. version, entrypoint, sessionKind
        self._tool_issuer: dict[str, str] = {}  # tool_use_id -> assistant row uuid (R36)
        self._last_wins: dict[str, Any] = {}
        self._appended_since_gc = 0
        self._gc_done_for_boundary: str | None = None

    # ------------------------------------------------------------ construction

    @classmethod
    def for_session(cls, ctx: "SessionContext", **kw: Any) -> "TranscriptStore":
        enabled = persistence_enabled(ctx.env, no_session_persistence=bool(ctx.extra.get("no_session_persistence")))
        return cls(ctx.transcript_path, session_id=ctx.session_id, cwd=str(ctx.cwd), enabled=enabled, **kw)

    @classmethod
    def resume(cls, path: Path, loaded: LoadedSession, *, cwd: str, **kw: Any) -> "TranscriptStore":
        """Continue appending to an existing file (or to a fork's new file)."""
        store = cls(path, session_id=loaded.session_id, cwd=cwd, **kw)
        store.last_uuid = loaded.leaf_uuid
        for it in loaded.items:
            if isinstance(it, Message) and it.role == "assistant":
                for tu in it.tool_uses():
                    store._tool_issuer[tu.id] = it.uuid
        store._last_wins = {k: v for k, v in loaded.metadata.items() if FOLD_RULES.get(k) == "last_wins"}
        return store

    # ------------------------------------------------------------ writing

    def _write(self, rows: list[dict[str, Any]]) -> None:
        if not self.enabled or not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(data)
        self._appended_since_gc += len(data.encode("utf-8"))

    def link(self, item: ConversationItem) -> None:
        """Assign ``parent_uuid`` per 05 R36 (without writing). Used directly by side
        chats, which keep the DAG in memory but never touch disk (05 R44)."""
        if is_compact_boundary(item):
            item.parent_uuid = None  # the chain walk stops here (05 §2.4)
        elif item.parent_uuid is None:
            parent = self.last_uuid
            if isinstance(item, Message) and item.role == "user":
                results = [b for b in item.content if isinstance(b, ToolResultBlock)]
                issuer = item.source_tool_assistant_uuid or next(
                    (self._tool_issuer[r.tool_use_id] for r in results if r.tool_use_id in self._tool_issuer), None
                )
                if issuer is not None:
                    parent = issuer
                    item.source_tool_assistant_uuid = issuer
            item.parent_uuid = parent
        if isinstance(item, Message) and item.role == "assistant":
            for tu in item.tool_uses():
                self._tool_issuer[tu.id] = item.uuid
        self.last_uuid = item.uuid

    def append(self, item: ConversationItem) -> None:
        """Assign ``parent_uuid`` per 05 R36 when unset, then append one row."""
        self.link(item)
        self._write([item_to_row(item, session_id=self.session_id, cwd=self.cwd, extra=self.extra_fields or None)])

    def append_metadata(self, type: str, value: Any) -> None:
        """Append a metadata row and re-append current last-wins metadata (05 R37)."""
        rule = FOLD_RULES.get(type, "last_wins")
        rows = []
        if rule == "last_wins":
            self._last_wins[type] = value
            # Re-append *every* current last-wins value so all of them sit at the tail.
            for t, v in self._last_wins.items():
                rows.append({"type": t, "value": v, "sessionId": self.session_id, "timestamp": now_iso()})
        else:
            rows.append({"type": type, "value": value, "sessionId": self.session_id, "timestamp": now_iso()})
        self._write(rows)

    def reappend_metadata(self) -> None:
        """Move the current last-wins metadata to the tail (call at session end)."""
        self._write(
            [{"type": t, "value": v, "sessionId": self.session_id, "timestamp": now_iso()} for t, v in self._last_wins.items()]
        )

    # ------------------------------------------------------------ reading

    def read_rows(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    # ------------------------------------------------------------ GC (05 §4.8)

    def maybe_gc(self) -> bool:
        """Rewrite the file keeping only what a resume can still reach. Returns True
        when a rewrite happened."""
        if not self.enabled or not self.path.exists():
            return False
        size = self.path.stat().st_size
        if size < self.gc_min_bytes:
            return False
        rows = self.read_rows()
        boundary_idx = _last_boundary_index(rows)
        boundary_trigger = (
            boundary_idx is not None
            and rows[boundary_idx]["uuid"] != self._gc_done_for_boundary
            and any(_is_real_message(r) for r in rows[boundary_idx + 1 :])
        )
        if not (boundary_trigger or self._appended_since_gc >= self.gc_backstop):
            return False
        keep = gc_keep_indices(rows)
        kept_rows = [r for i, r in enumerate(rows) if i in keep]
        data = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in kept_rows)
        _atomic_write(self.path, data)
        saved = 1 - len(data.encode("utf-8")) / size if size else 0.0
        self.gc_backstop = min(self.gc_backstop * 2, GC_BACKSTOP_MAX) if saved < GC_MIN_SAVED_FRAC else GC_BACKSTOP_START
        self._appended_since_gc = 0
        if boundary_idx is not None:
            self._gc_done_for_boundary = rows[boundary_idx]["uuid"]
        return True


# ---------------------------------------------------------------- helpers


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file; a torn last line (crash mid-write) is skipped."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and "type" in row:
                out.append(row)
    return out


def _atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _is_boundary_row(r: dict[str, Any]) -> bool:
    return r.get("type") == "system" and r.get("subtype") == "compact_boundary"


def _is_real_message(r: dict[str, Any]) -> bool:
    return r.get("type") in ("user", "assistant") and not r.get("isMeta")


def _last_boundary_index(rows: list[dict[str, Any]]) -> int | None:
    for i in range(len(rows) - 1, -1, -1):
        if _is_boundary_row(rows[i]):
            return i
    return None


def gc_keep_indices(rows: list[dict[str, Any]]) -> set[int]:
    """05 §4.8 keep set: rows after the last boundary, the preserved segment, dangling
    parent targets, the latest last-wins metadata, accumulate rows."""
    b = _last_boundary_index(rows)
    start = 0 if b is None else b
    keep: set[int] = set(range(start, len(rows)))
    index_of = {r["uuid"]: i for i, r in enumerate(rows) if r.get("type") in TRANSCRIPT_TYPES and "uuid" in r}
    # preserved segment: walk tail -> head
    if b is not None:
        seg = (rows[b].get("compactMetadata") or {}).get("preservedSegment")
        if seg:
            node, guard = seg.get("tailUuid"), set()
            while node and node in index_of and node not in guard:
                guard.add(node)
                keep.add(index_of[node])
                if node == seg.get("headUuid"):
                    break
                node = rows[index_of[node]].get("parentUuid")
    # dangling parents: a kept row whose parent would otherwise be dropped
    changed = True
    while changed:
        changed = False
        for i in list(keep):
            p = rows[i].get("parentUuid")
            if p and p in index_of and index_of[p] not in keep:
                keep.add(index_of[p])
                changed = True
    # metadata
    latest: dict[str, int] = {}
    for i, r in enumerate(rows):
        rule = FOLD_RULES.get(r.get("type", ""))
        if rule == "last_wins":
            latest[r["type"]] = i
        elif rule == "accumulate":
            keep.add(i)
    keep.update(latest.values())
    return keep


def fold_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold metadata rows per ``FOLD_RULES`` (05 §2.3)."""
    meta: dict[str, Any] = {}
    for r in rows:
        t = r.get("type", "")
        if "session_id" not in meta and r.get("sessionId"):
            meta["session_id"] = r["sessionId"]
        if _is_boundary_row(r):
            for k, rule in FOLD_RULES.items():
                if rule == "cleared_at_boundary":
                    meta.pop(k, None)
            continue
        rule = FOLD_RULES.get(t)
        if rule is None or rule == "transcript":
            continue
        if rule == "last_wins":
            meta[t] = _row_value(r)
        else:  # accumulate / cleared_at_boundary
            meta.setdefault(t, []).append(_row_value(r))
    return meta


def _leaf_from_meta(meta: dict[str, Any]) -> str | None:
    lp = meta.get("last-prompt")
    if isinstance(lp, Mapping):
        return lp.get("leafUuid")
    return lp if isinstance(lp, str) else None


def load_session(path: Path, *, at_uuid: str | None = None, fork: bool = False, new_session_id: str | None = None) -> LoadedSession:
    """05 §4.7: pick a leaf, walk parents (cycle-safe), replay content replacements,
    decide adopt-vs-fork for the session id (05 R42)."""
    from harnesslab.compaction.microcompact import replay_replacements  # own WP

    rows = read_jsonl(path)
    meta = fold_metadata(rows)
    by_id = {r["uuid"]: r for r in rows if r.get("type") in TRANSCRIPT_TYPES and "uuid" in r}

    leaf = at_uuid if at_uuid in by_id else None
    if leaf is None:
        cand = _leaf_from_meta(meta)
        leaf = cand if cand in by_id else None
    if leaf is None:
        main = [r for r in rows if r.get("type") in TRANSCRIPT_TYPES and not r.get("isSidechain")]
        conv = [r for r in main if r.get("type") in ("user", "assistant")]
        pick = (conv or main)[-1:] if (conv or main) else []
        leaf = pick[0]["uuid"] if pick else None

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cycle = False
    node = by_id.get(leaf) if leaf else None
    while node is not None:
        if node["uuid"] in seen:
            cycle = True
            break
        seen.add(node["uuid"])
        chain.append(node)
        parent = node.get("parentUuid")
        node = by_id.get(parent) if parent else None
    chain.reverse()
    items = [row_to_item(r) for r in chain]
    items = replay_replacements(items, meta.get("content-replacement", []))

    original = meta.get("session_id") or path.stem
    adoptable = bool(meta.get("session_id"))
    forked = fork or not adoptable
    sid = (new_session_id or new_uuid()) if forked else original
    return LoadedSession(sid, items, meta, leaf, forked, cycle)


def write_fork(loaded: LoadedSession, path: Path, *, cwd: str) -> TranscriptStore:
    """Persist a forked chain into a new transcript file (05 R42/R43). Rows keep
    their uuids so the fork's history is byte-identical to the source branch."""
    store = TranscriptStore(path, session_id=loaded.session_id, cwd=cwd)
    for it in loaded.items:
        store.append(it)
    for k, v in loaded.metadata.items():
        if FOLD_RULES.get(k) == "accumulate":
            for x in v:
                store.append_metadata(k, x)
    store.append_metadata("last-prompt", {"leafUuid": store.last_uuid})
    return store


def _head_tail_rows(path: Path) -> list[dict[str, Any]]:
    """Read only the first and last 64 KiB (05 R37), parsing complete lines."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size <= 2 * PICKER_WINDOW:
            chunks = [fh.read()]
        else:
            head = fh.read(PICKER_WINDOW)
            fh.seek(size - PICKER_WINDOW)
            tail = fh.read()
            chunks = [head.rsplit(b"\n", 1)[0], tail.split(b"\n", 1)[1] if b"\n" in tail else b""]
    rows = []
    for chunk in chunks:
        for line in chunk.decode("utf-8", "replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def list_sessions(project_dir: Path, *, real_cwd: str | None = None) -> list[SessionSummary]:
    """Resume-picker entries, newest first, with the 05 R41 filters."""
    out: list[SessionSummary] = []
    if not project_dir.is_dir():
        return out
    for p in project_dir.glob("*.jsonl"):
        rows = _head_tail_rows(p)
        conv = [r for r in rows if r.get("type") in TRANSCRIPT_TYPES]
        if not conv:
            continue
        first = conv[0]
        if first.get("isSidechain") or first.get("sessionKind") in HIDDEN_SESSION_KINDS:
            continue
        if str(first.get("entrypoint", "")).startswith("sdk"):
            continue
        if any(r.get("type") == "superseded-by" for r in rows):
            continue
        if real_cwd is not None and first.get("cwd") not in (None, real_cwd):
            continue
        meta = fold_metadata(rows)
        title = meta.get("custom-title") or meta.get("ai-title") or meta.get("summary")
        preview = ""
        for r in conv:
            if r.get("type") == "user" and not r.get("isMeta"):
                item = row_to_item(r)
                if isinstance(item, Message) and item.text().strip():
                    preview = item.text().strip().replace("\n", " ")[:PREVIEW_CHARS]
                    break
        out.append(SessionSummary(first.get("sessionId") or p.stem, p, title if isinstance(title, str) else None, preview, p.stat().st_mtime))
    out.sort(key=lambda s: s.modified, reverse=True)
    return out


def rewind_reminder(files_reverted: bool) -> str:
    """05 R43 / R47: conversation was rewound; files were / were not reverted;
    shell-made changes are never tracked; check VCS status."""
    files = (
        "Files edited through the harness's file tools were restored to how they were at that point."
        if files_reverted
        else "Files on disk were NOT restored: they still contain every change made after that point."
    )
    return (
        "The user rewound this conversation to an earlier point; later messages are gone. "
        f"{files} Changes made through shell commands are never tracked and were not undone. "
        "Check the version-control status before assuming what the files contain."
    )


def rewind_reminder_attachment(files_reverted: bool) -> Attachment:
    return Attachment("rewind", {"files_reverted": files_reverted, "text": rewind_reminder(files_reverted)})
