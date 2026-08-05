"""Session storage abstraction — InMemory (Colab) vs Redis (AWS).

Colab: single-process, no persistence needed → InMemorySessionStore.
AWS: multi-instance, sticky LB → RedisSessionStore for cross-instance state.

All methods are async for smooth porting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class SessionStore(ABC):
    """Abstract session storage."""

    @abstractmethod
    async def get(self, session_id: str) -> Optional[dict]:
        """Retrieve session data (any JSON-serializable dict)."""
        ...

    @abstractmethod
    async def set(self, session_id: str, data: dict, ttl_seconds: Optional[int] = None) -> None:
        """Store session data."""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Remove session data. Returns True if existed."""
        ...

    @abstractmethod
    async def exists(self, session_id: str) -> bool:
        ...


class InMemorySessionStore(SessionStore):
    """Simple dict-based store for single-process Colab."""

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


class RedisSessionStore(SessionStore):
    """Redis-backed store for multi-instance AWS.

    Requires: pip install redis[hiredis]
    Expects env: REDIS_URL (e.g. redis://:pass@host:6379/0)
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url
        self._client: Optional[Any] = None
        self._ttl = 3600 * 24  # default 24h

    async def _ensure(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url or "redis://localhost:6379/0")
        return self._client

    async def get(self, session_id: str) -> Optional[dict]:
        client = await self._ensure()
        raw = await client.get(f"session:{session_id}")
        if raw is None:
            return None
        import json

        return json.loads(raw)

    async def set(self, session_id: str, data: dict, ttl_seconds: Optional[int] = None) -> None:
        client = await self._ensure()
        import json

        await client.set(
            f"session:{session_id}",
            json.dumps(data),
            ex=ttl_seconds or self._ttl,
        )

    async def delete(self, session_id: str) -> bool:
        client = await self._ensure()
        result = await client.delete(f"session:{session_id}")
        return result > 0

    async def exists(self, session_id: str) -> bool:
        client = await self._ensure()
        return await client.exists(f"session:{session_id}") > 0
