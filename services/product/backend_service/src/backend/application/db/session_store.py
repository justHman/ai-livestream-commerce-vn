"""Session-store contract (canonical, OpenSpec 1.23).

The persistence port for JSON-serializable session metadata. Memory, Redis,
and Postgres adapters live beside this module; the legacy ``core.store``
seam re-exports them for existing callers until Task 1.26.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

__all__ = ["SessionLockTimeout", "SessionStore"]


class SessionLockTimeout(Exception):
    """Bounded wait for the per-session lock expired (P1-04).

    Raised by stores that provide a distributed ``with_session_lock`` when the
    lock cannot be acquired within ``acquire_timeout_seconds``. Callers must
    treat this as a 503 (session busy) and never proceed unlocked.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"timed out acquiring session lock: {session_id}")
        self.session_id = session_id


class SessionStore(ABC):
    """Abstract session storage (async contract)."""

    @abstractmethod
    async def get(self, session_id: str) -> Optional[dict]: ...

    @abstractmethod
    async def set(self, session_id: str, data: dict, ttl_seconds: Optional[int] = None) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool: ...

    @abstractmethod
    async def exists(self, session_id: str) -> bool: ...

    def exists_sync(self, session_id: str) -> bool:
        """Sync convenience for route-layer guards; default False.

        Subclasses backed by in-process state may override with a true sync
        check. Remote stores keep the async contract.
        """
        return False
