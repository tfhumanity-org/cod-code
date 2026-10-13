"""Transcript row envelope and (de)serialization (FINAL - architect).

Spec coverage
-------------
* 05 §2.3  JSONL row envelope; field names are interface-level and preserved
           (``parentUuid``, ``isSidechain``, ``isMeta``, ...).
* 05 §2.3  metadata row types and their fold rules (``FOLD_RULES``).
* 05 §2.4  compact boundary metadata (``CompactMetadata``).
* 05 §2.5  file-history snapshot (``FileBackup`` / ``FileSnapshot``).
* 02 §2.3  attachments persist as ``type: "attachment"`` rows.

``item_to_row`` / ``row_to_item`` convert between the in-memory
``Message``/``Attachment`` and the on-disk dict, losslessly for every flag the
harness uses. The store (``transcript.store``, WP4) only appends/reads dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from harnesslab.types import (
    ApiErrorDetail,
    Attachment,
    ConversationItem,
    ErrorKind,
    Message,
    Usage,
    block_from_api,
    now_iso,
)

#: Rows that form the uuid DAG.
TRANSCRIPT_TYPES = {"user", "assistant", "system", "attachment"}

FoldRule = Literal["transcript", "last_wins", "accumulate", "cleared_at_boundary"]

FOLD_RULES: dict[str, FoldRule] = {
    **{t: "transcript" for t in TRANSCRIPT_TYPES},
    **{
        t: "last_wins"
        for t in (
            "summary",
            "custom-title",
            "ai-title",
            "tag",
            "mode",
            "permission-mode",
            "worktree-state",
            "cost-state",
            "pr-link",
            "agent-name",
            "agent-color",
            "agent-setting",
            "last-prompt",
        )
    },
    **{t: "accumulate" for t in ("content-replacement", "fork-context-ref", "memory-mode")},
    **{
        t: "cleared_at_boundary"
        for t in ("progress", "file-history-snapshot", "file-history-delta", "api-request")
    },
}


@dataclass
class CompactMetadata:
    """05 §2.4 ``compactMetadata``."""

    trigger: Literal["auto", "manual"]
    pre_tokens: int = 0
    post_tokens: int = 0
    duration_ms: int = 0
    pre_compact_discovered_tools: list[str] = field(default_factory=list)
    preserved_segment: dict[str, str] | None = None  # {headUuid, anchorUuid, tailUuid}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "trigger": self.trigger,
            "preTokens": self.pre_tokens,
            "postTokens": self.post_tokens,
            "durationMs": self.duration_ms,
            "preCompactDiscoveredTools": list(self.pre_compact_discovered_tools),
        }
        if self.preserved_segment:
            d["preservedSegment"] = dict(self.preserved_segment)
        return d


@dataclass
class FileBackup:
    """05 §2.5 one tracked file. ``backup_file_name=None`` => file did not exist."""

    backup_file_name: str | None
    version: int
    backup_time: str = field(default_factory=now_iso)
    real_parent_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backupFileName": self.backup_file_name,
            "version": self.version,
            "backupTime": self.backup_time,
            "realParentDir": self.real_parent_dir,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FileBackup":
        return FileBackup(d.get("backupFileName"), int(d.get("version", 1)), d.get("backupTime", ""), d.get("realParentDir", ""))


@dataclass
class FileSnapshot:
    message_id: str
    tracked_file_backups: dict[str, FileBackup] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "trackedFileBackups": {p: b.to_dict() for p, b in self.tracked_file_backups.items()},
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FileSnapshot":
        return FileSnapshot(
            d["messageId"],
            {p: FileBackup.from_dict(b) for p, b in d.get("trackedFileBackups", {}).items()},
            d.get("timestamp", ""),
        )


def item_to_row(item: ConversationItem, *, session_id: str, cwd: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a message/attachment into a 05 §2.3 row dict."""
    row: dict[str, Any] = {
        "uuid": item.uuid,
        "parentUuid": item.parent_uuid,
        "timestamp": item.timestamp,
        "sessionId": session_id,
        "cwd": cwd,
    }
    if isinstance(item, Attachment):
        row.update(type="attachment", attachment={"type": item.type, **item.payload})
    else:
        m = item
        row["type"] = m.role
        row["isSidechain"] = m.is_sidechain
        msg: dict[str, Any] = {"role": m.role, "content": [b.to_api() for b in m.content]}
        if m.role == "assistant":
            msg.update(stop_reason=m.stop_reason, model=m.model, id=m.message_id)
            if m.usage is not None:
                msg["usage"] = m.usage.to_dict()
        row["message"] = msg
        for flag, key in (
            ("is_meta", "isMeta"),
            ("is_compact_summary", "isCompactSummary"),
            ("is_api_error", "isApiErrorMessage"),
            ("aborted_mid_stream", "abortedMidStream"),
        ):
            if getattr(m, flag):
                row[key] = True
        if m.api_error is not None:
            row["apiError"] = {
                "kind": m.api_error.kind.value,
                "message": m.api_error.message,
                "status": m.api_error.status,
                "actualTokens": m.api_error.actual_tokens,
                "limitTokens": m.api_error.limit_tokens,
            }
        if m.source_tool_assistant_uuid:
            row["sourceToolAssistantUUID"] = m.source_tool_assistant_uuid
        if m.tool_use_result is not None:
            row["toolUseResult"] = m.tool_use_result
        if m.origin:
            row["origin"] = m.origin
        if m.subtype:
            row["subtype"] = m.subtype
        if "logicalParentUuid" in m.extra:
            row["logicalParentUuid"] = m.extra["logicalParentUuid"]
        if "compactMetadata" in m.extra:
            row["compactMetadata"] = m.extra["compactMetadata"]
    if extra:
        row.update(extra)
    return row


def row_to_item(row: dict[str, Any]) -> ConversationItem:
    """Inverse of ``item_to_row`` for transcript rows (``FOLD_RULES[type] == 'transcript'``)."""
    t = row["type"]
    if t == "attachment":
        payload = dict(row.get("attachment", {}))
        a_type = payload.pop("type", "unknown")
        return Attachment(a_type, payload, row["uuid"], row.get("parentUuid"), row.get("timestamp", ""))
    msg = row.get("message", {})
    usage = Usage(**msg["usage"]) if msg.get("usage") else None
    api_error = None
    if row.get("apiError"):
        e = row["apiError"]
        api_error = ApiErrorDetail(ErrorKind(e["kind"]), e.get("message", ""), e.get("status"), e.get("actualTokens"), e.get("limitTokens"))
    extra: dict[str, Any] = {}
    for k in ("logicalParentUuid", "compactMetadata"):
        if k in row:
            extra[k] = row[k]
    return Message(
        role=t,
        content=[block_from_api(b) for b in msg.get("content", [])],
        uuid=row["uuid"],
        parent_uuid=row.get("parentUuid"),
        timestamp=row.get("timestamp", ""),
        is_meta=bool(row.get("isMeta")),
        is_api_error=bool(row.get("isApiErrorMessage")),
        api_error=api_error,
        stop_reason=msg.get("stop_reason"),
        usage=usage,
        model=msg.get("model"),
        message_id=msg.get("id"),
        aborted_mid_stream=bool(row.get("abortedMidStream")),
        is_compact_summary=bool(row.get("isCompactSummary")),
        is_sidechain=bool(row.get("isSidechain")),
        source_tool_assistant_uuid=row.get("sourceToolAssistantUUID"),
        tool_use_result=row.get("toolUseResult"),
        origin=row.get("origin"),
        subtype=row.get("subtype"),
        extra=extra,
    )
