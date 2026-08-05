"""Redis session store (canonical, OpenSpec 1.23).

Multi-instance store for AWS (sticky LB). Requires ``redis``; expects
REDIS_URL. Async contract; sync ``exists`` is unavailable over the network.
"""

from __future__ import annotations

import json
from typing import Optional

from .session_store import SessionStore

__all__ = ["RedisSessionStore"]


class RedisSessionStore(SessionStore):
    """Redis-backed store for multi-instance AWS."""

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url
        self._client = None
        self._ttl = 3600 * 24

    async def _ensure(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url or "redis://localhost:6379/0")
        return self._client

    async def get(self, session_id: str) -> Optional[dict]:
        client = await self._ensure()
        raw = await client.get(f"session:{session_id}")
        return json.loads(raw) if raw is not None else None

    async def set(self, session_id: str, data: dict, ttl_seconds: Optional[int] = None) -> None:
        client = await self._ensure()
        await client.set(f"session:{session_id}", json.dumps(data), ex=ttl_seconds or self._ttl)

    async def delete(self, session_id: str) -> bool:
        client = await self._ensure()
        return (await client.delete(f"session:{session_id}")) > 0

    async def exists(self, session_id: str) -> bool:
        client = await self._ensure()
        return (await client.exists(f"session:{session_id}")) > 0
