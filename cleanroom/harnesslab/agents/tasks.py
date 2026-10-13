"""Per-session task registry for background work (FINAL - architect).

Spec 06 §2.1: everything async (subagents, background shells, monitors, MCP
auto-background, workflows, cron fires) is registered in one per-session task
registry, and its completion is delivered through one notification queue
(``agents.notifications``). Shared by WP2 (Bash ``run_in_background``,
auto-background on timeout, 03 R40/R41) and WP5 (agents, TaskStop).

Implemented now because two work packages depend on it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from harnesslab.types import now_iso
from harnesslab.util.ids import short_id

TaskKind = Literal["agent", "shell", "monitor", "mcp", "workflow", "cron", "wakeup"]
TaskStatus = Literal["running", "completed", "failed", "killed", "blocked"]


@dataclass
class BackgroundTask:
    id: str
    kind: TaskKind
    description: str
    status: TaskStatus = "running"
    output_file: Path | None = None
    result: Any = None
    error: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    name: str | None = None  # addressable agent name (06 §2.2)
    owner_agent_id: str | None = None
    stop: Callable[[], None] | None = None  # how to kill it (TaskStop, 06 R29)
    meta: dict[str, Any] = field(default_factory=dict)


class TaskRegistry:
    """Thread-safe id -> task map. ``on_finish`` callbacks enqueue notifications."""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[BackgroundTask], None]] = []

    def register(self, kind: TaskKind, description: str, **kw: Any) -> BackgroundTask:
        prefix = {"agent": "a", "shell": "b", "monitor": "m", "mcp": "p", "workflow": "wf_", "cron": "c", "wakeup": "w"}[kind]
        task = BackgroundTask(id=short_id(prefix), kind=kind, description=description, **kw)
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id_or_name: str) -> BackgroundTask | None:
        with self._lock:
            if task_id_or_name in self._tasks:
                return self._tasks[task_id_or_name]
            return next((t for t in self._tasks.values() if t.name == task_id_or_name), None)

    def list(self, *, status: TaskStatus | None = None) -> list[BackgroundTask]:
        with self._lock:
            return [t for t in self._tasks.values() if status is None or t.status == status]

    def running_count(self, kind: TaskKind | None = None) -> int:
        return sum(1 for t in self.list(status="running") if kind is None or t.kind == kind)

    def on_finish(self, fn: Callable[[BackgroundTask], None]) -> None:
        self._listeners.append(fn)

    def finish(self, task_id: str, status: TaskStatus, *, result: Any = None, error: str | None = None) -> BackgroundTask:
        with self._lock:
            task = self._tasks[task_id]
            if task.status != "running":
                return task
            task.status, task.result, task.error, task.finished_at = status, result, error, now_iso()
        for fn in list(self._listeners):
            fn(task)
        return task

    def stop(self, task_id_or_name: str) -> BackgroundTask | None:
        task = self.get(task_id_or_name)
        if task is None or task.status != "running":
            return task
        if task.stop is not None:
            task.stop()
        return self.finish(task.id, "killed")
