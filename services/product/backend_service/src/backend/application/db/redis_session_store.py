"""Redis session store (canonical, OpenSpec 1.23).

Multi-instance store for AWS (sticky LB). Requires ``redis``; expects
REDIS_URL. Async contract; sync ``exists`` is unavailable over the network.

P1-04: provides a distributed per-session lock (Redis ``SET NX``) that
``PlatformEventIngestionService`` uses to serialize the dedup critical section
across processes (rolling deploy, autoscale, operator). The lock TTL bounds a
crashed holder; the Lua-guarded release never deletes a newer holder's lock.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from .session_store import SessionLockTimeout, SessionStore

__all__ = ["RedisSessionStore"]

_LOCK_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


class RedisSessionStore(SessionStore):
    """Redis-backed store for multi-instance AWS."""

    def __init__(self, url: Optional[str] = None, client: Any = None) -> None:
        self._url = url
        # Injectable for tests; None -> lazy real redis.asyncio client.
        self._client = client
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

    # ------------------------------------------------------------------
    # Distributed per-session lock (P1-04)
    # ------------------------------------------------------------------

    def _lock_key(self, session_id: str) -> str:
        return f"session:{session_id}:lock"

    async def acquire_session_lock(
        self, session_id: str, token: str, ttl_seconds: float = 10.0
    ) -> bool:
        """Atomically claim the per-session lock; True for exactly one caller."""
        client = await self._ensure()
        acquired = await client.set(
            self._lock_key(session_id), token, nx=True, px=int(ttl_seconds * 1000)
        )
        return bool(acquired)

    async def release_session_lock(self, session_id: str, token: str) -> None:
        """Release the lock only when we still hold it (compare-and-delete)."""
        client = await self._ensure()
        await client.eval(_LOCK_RELEASE_SCRIPT, 1, self._lock_key(session_id), token)

    @asynccontextmanager
    async def with_session_lock(
        self,
        session_id: str,
        *,
        ttl_seconds: float = 10.0,
        acquire_timeout_seconds: float = 2.0,
    ) -> AsyncIterator[None]:
        """Hold the per-session lock across the ``async with`` body.

        Acquires with bounded retry (~20ms backoff) and raises
        ``SessionLockTimeout`` when the lock is not gained in time — callers
        must never proceed unlocked. Always releases on exit.
        """
        token = secrets.token_hex(16)
        deadline = time.monotonic() + acquire_timeout_seconds
        while True:
            if await self.acquire_session_lock(session_id, token, ttl_seconds=ttl_seconds):
                break
            if time.monotonic() >= deadline:
                raise SessionLockTimeout(session_id)
            await asyncio.sleep(0.02)
        try:
            yield
        finally:
            await self.release_session_lock(session_id, token)
