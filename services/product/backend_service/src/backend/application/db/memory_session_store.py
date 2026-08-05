"""In-memory session store (canonical, OpenSpec 1.23).

Single-process store for dev/Colab. Async contract; ``exists_sync`` is
overridden because the state is in-process.
"""

from __future__ import annotations

from typing import Optional

from .session_store import SessionStore

__all__ = ["InMemorySessionStore"]


class InMemorySessionStore(SessionStore):
    """Dict-based store for single-process runs."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def get(self, session_id: str) -> Optional[dict]:
        return self._store.get(session_id)

    async def set(self, session_id: str, data: dict, ttl_seconds: Optional[int] = None) -> None:
        self._store[session_id] = data

    async def delete(self, session_id: str) -> bool:
        existed = session_id in self._store
        self._store.pop(session_id, None)
        return existed

    async def exists(self, session_id: str) -> bool:
        return session_id in self._store

    def exists_sync(self, session_id: str) -> bool:
        return session_id in self._store
