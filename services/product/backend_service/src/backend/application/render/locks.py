"""Per-session lock registry (Task 8).

Provides non-blocking try-acquire so a second concurrent /lite/say on an
already-speaking session is rejected with 409 instead of blocking or
double-running. Single-process only; multi-instance coordination (Redis
locks) is deferred to post-P0.

Implementation note: uses ``threading.Lock`` (not ``asyncio.Lock``) because
``asyncio.Lock.acquire`` is a coroutine and offers no non-blocking variant
in Python 3.10. The event loop is single-threaded, so a ``threading.Lock``
acquired/released on the loop thread provides the same re-entry guard
across await points without requiring ``await``. The lock is only ever
touched on the main event-loop thread (in /lite/say and /lite/interrupt),
never from the orchestrator's worker thread.

Public surface:
  SessionLockRegistry
    get(session_id) -> threading.Lock       (create-or-return)
    try_acquire(session_id) -> bool          (non-blocking; False = already held)
    release(session_id) -> None              (release the holder's lock)
    drop(session_id) -> None                 (remove the entry on session stop)
"""

from __future__ import annotations

import threading
from typing import Dict


class SessionLockRegistry:
    """Per-session non-blocking lock registry.

    Each session_id gets its own ``threading.Lock`` lazily created on first
    access. ``try_acquire`` is the non-blocking entry point used by /lite/say
    to reject concurrent says with 409. The holder releases the lock via
    ``release`` when the turn finishes. ``drop`` removes the entry entirely
    when a session is stopped.

    Single-process; no cross-instance coordination (post-P0: Redis).
    """

    def __init__(self) -> None:
        self._locks: Dict[str, threading.Lock] = {}

    def get(self, session_id: str) -> threading.Lock:
        """Return the lock for ``session_id``, creating it if needed."""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            self._locks[session_id] = lock
        return lock

    def try_acquire(self, session_id: str) -> bool:
        """Non-blocking acquire. Returns True if acquired (caller must release),
        False if the lock is already held (caller does NOT hold it — do not
        release in this case).
        """
        lock = self.get(session_id)
        # ``threading.Lock.acquire(blocking=False)`` returns True on success,
        # False if already held. Never blocks.
        return lock.acquire(blocking=False)

    def release(self, session_id: str) -> None:
        """Release the lock held for ``session_id``.

        No-op if there is no lock entry for the session (e.g. never acquired).
        Safe to call in a ``finally`` block.
        """
        lock = self._locks.get(session_id)
        if lock is None:
            return
        # ``threading.Lock.release`` raises RuntimeError if not held; guard
        # with ``locked()`` so a double-release is a no-op.
        if lock.locked():
            lock.release()

    def drop(self, session_id: str) -> None:
        """Remove the lock entry for ``session_id`` (called on session stop).

        Does NOT release a held lock — the holder is responsible for releasing
        before stopping. Safe to call multiple times.
        """
        self._locks.pop(session_id, None)

    def is_locked(self, session_id: str) -> bool:
        """Diagnostic: True if the session's lock exists and is currently held."""
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()


__all__ = ["SessionLockRegistry"]
