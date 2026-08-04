"""Session-store contract (canonical, OpenSpec 1.23).

The persistence port for JSON-serializable session metadata. Memory, Redis,
and Postgres adapters live beside this module; the legacy ``core.store``
seam re-exports them for existing callers until Task 1.26.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

__all__ = ["SessionStore"]


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
