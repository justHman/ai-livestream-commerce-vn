"""Layer-1 shared logical quota (C.2 / R8.10).

The old limiter (``backend.application.render.limiters.SlidingWindowLimiter``)
was process-local and keyed by client IP first — quota multiplied across
replicas and authenticated users behind NAT shared one IP bucket. This module
is the logical-quota layer: one ``SharedQuotaLimiter`` per deployment over one
``RateLimitStore``. A store is the shareable counter (in-memory for a single
replica, Redis once replicas scale past one); the limiter holds the per-scope
budget. REST quota keys derive from the authenticated identity (see
``quota_identity_key``) so NAT users do not share a bucket.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import threading
import time
from typing import Any, Protocol

from backend.api.security.authentication import parse_bearer, tokens_match

__all__ = [
    "InMemoryRateLimitStore",
    "RateLimitStore",
    "RedisRateLimitStore",
    "SharedQuotaLimiter",
    "quota_identity_key",
]


class RateLimitStore(Protocol):
    """Async, shareable logical-quota counter keyed by ``key``."""

    async def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        """Record ``key`` and return whether it is still under ``limit``."""
        ...


class InMemoryRateLimitStore:
    """Process-local sliding-window counter (single-replica shared quota).

    Same sliding-window semantics as ``SlidingWindowLimiter`` but with per-call
    limit/window so one store serves every scope. Thread-safe and bounded.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    async def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._events.popitem(last=False)
                events = deque()
                self._events[key] = events
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            self._events.move_to_end(key)
            return True


class RedisRateLimitStore:
    """Redis-backed shared quota for multi-replica deployments (AWS).

    Fixed-window atomic Lua counter: INCR and set PEXPIRE only on first hit so
    the key always expires and never grows unbounded. Redis is required at
    runtime only when a real URL is used; a fake client is injectable for tests.
    """

    _SCRIPT = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return c
"""

    def __init__(self, url: str | None = None, *, client: Any = None) -> None:
        self._url = url
        self._client = client

    async def _ensure(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url or "redis://localhost:6379/0")
        return self._client

    async def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        client = await self._ensure()
        count = await client.eval(self._SCRIPT, 1, f"rl:{key}", int(window_seconds * 1000))
        return int(count) <= limit


# ponytail: fixed-window counting permits up to 2x burst across a window
# boundary; switch to a sliding-window ZSET if exact fairness matters.


class SharedQuotaLimiter:
    """One logical quota across replicas: delegate to a shared store."""

    def __init__(self, store, *, requests_limit: int, window_seconds: float) -> None:
        self._store = store
        self._requests_limit = requests_limit
        self._window_seconds = window_seconds

    async def allow(self, key: str) -> bool:
        return await self._store.allow(
            key, limit=self._requests_limit, window_seconds=self._window_seconds
        )


def quota_identity_key(request, cfg) -> str:
    """Derive a rate-limit identity: authenticated role beats a shared NAT IP.

    Precedence: admin bearer, then viewer bearer, then IP (honouring the
    explicit ``trusted_proxy_client_ip`` policy). Never includes token bytes.
    """
    bearer = parse_bearer(request.headers.get("authorization"))
    if bearer is not None:
        admin_token = getattr(cfg, "admin_api_token", "")
        if admin_token and tokens_match(bearer, admin_token):
            return "id:admin"
        viewer_token = getattr(cfg, "backend_api_token", "")
        if viewer_token and tokens_match(bearer, viewer_token):
            return "id:viewer"
    if getattr(cfg, "trusted_proxy_client_ip", False):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return f"ip:{first}"
    host = request.client.host if request.client is not None else "unknown"
    return f"ip:{host}"
