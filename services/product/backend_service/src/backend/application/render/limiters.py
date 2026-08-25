"""Bounded in-process request controls for the ASGI control plane.

Copied from ``core/api/limits.py`` (COPY-DON'T-IMPORT, OpenSpec 1.21) so the
canonical backend service is self-contained. Stdlib only.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any


class SlidingWindowLimiter:
    """Thread-safe, memory-bounded sliding-window limiter."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record and allow ``key`` unless it reached the window limit."""
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._events.popitem(last=False)
                events = deque()
                self._events[key] = events
            self._prune(events, current)
            if len(events) >= self._limit:
                return False
            events.append(current)
            self._events.move_to_end(key)
            return True

    def _prune(self, events: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while events and events[0] <= cutoff:
            events.popleft()


class WebSocketLimiters:
    """Apply independent bounded budgets per WebSocket connection and session."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int) -> None:
        self._connection = SlidingWindowLimiter(limit, window_seconds, max_keys)
        self._session = SlidingWindowLimiter(limit, window_seconds, max_keys)

    def allow(self, connection_key: str, session_key: str) -> bool:
        """Apply the connection budget before the session budget.

        A session rejection can consume a connection slot; the bounded counters
        remain independently correct and are protected by their own locks.
        """
        return self._connection.allow(connection_key) and self._session.allow(session_key)


ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class MaxBodySizeMiddleware:
    """Reject HTTP requests whose streamed ASGI body exceeds ``max_bytes``."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def receive_body() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, receive_body, send)

    @staticmethod
    async def _send_too_large(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        payload = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


# Layer-2 LOCAL overload protection (per replica); logical quotas use backend.application.rate_limit.
