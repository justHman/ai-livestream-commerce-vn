"""backend.api.middleware.body_limit — bounded HTTP request body size rejection.

Rejects both fixed-length (declared ``Content-Length``) and streamed/chunked
bodies with ``413`` BEFORE route business logic runs, without buffering
unbounded data.  Malformed/negative length headers fail safely (the stream
is still bounded by ``max_bytes`` and oversized chunks are rejected).

This is a pure ASGI middleware: it drains the receive stream in bounded
chunks, counts bytes, and — once the cap is crossed — responds 413 and never
invokes the downstream app (so route logic/validation never sees the body).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], ASGIReceive, ASGISend], Awaitable[None]]

_STATUS_TOO_LARGE = 413


class BodyLimitMiddleware:
    """Reject HTTP bodies larger than ``max_bytes`` at the ASGI boundary."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared >= 0 and declared > self.max_bytes:
                await self._send_too_large(send)
                return

        # Bounded streaming read: count bytes across chunks; reject once the
        # cumulative size crosses ``max_bytes`` (chunked bodies without a
        # declared length are caught here).
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
    async def _send_too_large(send: ASGISend) -> None:
        payload = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": _STATUS_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
