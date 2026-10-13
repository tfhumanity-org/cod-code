"""The single notification queue for all async sources (FINAL - architect).

Spec coverage
-------------
* 06 §2.1 / 06#T10  one queue for agents, shells, monitors, MCP auto-background,
  cron and wakeups.
* 06 §2.4  ``<task-notification>`` rendering (task-id, status, summary, result,
  usage), queued with mode ``task-notification`` and priority ``next``.
* 06 §4.4  drained between tool rounds and when idle.
* 03 R65   notifications are labelled as not user input and escape closing
  reminder tags.

Implemented now because WP2 (background Bash) and WP5 (agents) both enqueue.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from harnesslab.types import Message, TextBlock
from harnesslab.util.text import escape_reminder_close, wrap_system_reminder

Priority = Literal["now", "next", "later"]
_ORDER = {"now": 0, "next": 1, "later": 2}


@dataclass
class QueuedMessage:
    message: Message
    priority: Priority = "next"
    mode: str = "task-notification"
    seq: int = 0


def render_task_notification(
    task_id: str,
    status: str,
    summary: str,
    result: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    """Render the 06 §2.4 envelope. Content is escaped (03 R65)."""
    parts = [
        "<task-notification>",
        f"<task-id>{escape_reminder_close(task_id)}</task-id>",
        f"<status>{escape_reminder_close(status)}</status>",
        f"<summary>{escape_reminder_close(summary)}</summary>",
    ]
    if result is not None:
        parts.append(f"<result>{escape_reminder_close(result)}</result>")
    if usage:
        u = ", ".join(f"{k}: {v}" for k, v in usage.items())
        parts.append(f"<usage>{escape_reminder_close(u)}</usage>")
    parts.append("</task-notification>")
    return "\n".join(parts)


def notification_message(body: str) -> Message:
    """User-role meta message, labelled as not typed by the user (03 R65)."""
    text = "This is an automated background notification, not a message from the user.\n" + body
    return Message("user", [TextBlock(wrap_system_reminder(text))], is_meta=True, origin="task-notification")


class NotificationQueue:
    """Thread-safe priority queue of messages for the owner's next turn."""

    def __init__(self) -> None:
        self._items: list[QueuedMessage] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._wakeup = threading.Event()

    def push(self, message: Message, *, priority: Priority = "next", mode: str = "task-notification") -> None:
        with self._lock:
            self._seq += 1
            self._items.append(QueuedMessage(message, priority, mode, self._seq))
            self._items.sort(key=lambda q: (_ORDER[q.priority], q.seq))
        self._wakeup.set()

    def notify(self, task_id: str, status: str, summary: str, result: str | None = None, usage: dict[str, Any] | None = None) -> None:
        self.push(notification_message(render_task_notification(task_id, status, summary, result, usage)))

    def drain(self, *, include_later: bool = True) -> list[Message]:
        """Pop everything (06 §4.4). ``include_later=False`` leaves ``later`` items."""
        with self._lock:
            keep = [] if include_later else [q for q in self._items if q.priority == "later"]
            out = [q.message for q in self._items if include_later or q.priority != "later"]
            self._items = keep
            self._wakeup.clear()
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until something is queued (idle sessions wake on notifications)."""
        return self._wakeup.wait(timeout)
