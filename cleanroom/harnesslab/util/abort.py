"""Abort controllers with parent/child linking (FINAL - architect).

Implements spec 01 R12 / R51 and 03 R13: one controller per turn, one child per
tool call; aborting the parent aborts every child, aborting a child does not
touch the parent or siblings.

Python has no built-in AbortController, so this is a tiny thread-safe one.
"""

from __future__ import annotations

import threading
from typing import Callable


class AbortedError(Exception):
    """Raised by ``AbortSignal.raise_if_aborted`` (and by code honouring a signal)."""

    def __init__(self, reason: str = "aborted") -> None:
        super().__init__(reason)
        self.reason = reason


class AbortSignal:
    """Read side of an ``AbortController``."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.Lock()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def add_listener(self, fn: Callable[[str], None]) -> None:
        """Call ``fn(reason)`` on abort (immediately if already aborted)."""
        with self._lock:
            if not self._event.is_set():
                self._listeners.append(fn)
                return
        fn(self._reason or "aborted")

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep up to ``timeout`` seconds; return True if aborted meanwhile.
        Used for interruptible retry sleeps (01 R24)."""
        return self._event.wait(timeout)

    def raise_if_aborted(self) -> None:
        if self._event.is_set():
            raise AbortedError(self._reason or "aborted")

    def _fire(self, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            listeners, self._listeners = self._listeners, []
        for fn in listeners:
            fn(reason)


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: str = "aborted") -> None:
        self.signal._fire(reason)

    def child(self) -> "AbortController":
        """A controller aborted whenever this one is (01 R12)."""
        c = AbortController()
        self.signal.add_listener(c.abort)
        return c
