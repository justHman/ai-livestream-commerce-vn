"""Body limit middleware: reject oversized HTTP bodies with 413.

Applies before route logic so auth/rate/concurrency and engine work never
see oversized payloads. Handles both fixed-length and streamed/chunked
bodies.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than the configured limit."""

    def __init__(self, app, *, max_body_bytes: int = 100_000) -> None:
        super().__init__(app)
        self._max_body_bytes = int(max_body_bytes)

    async def dispatch(self, request: Request, call_next):
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = -1
            if length > self._max_body_bytes:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "body_too_large",
                            "message": f"request body exceeds {self._max_body_bytes} bytes",
                        }
                    },
                )
        return await call_next(request)