"""Access logging middleware for the avatar service."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from avatar.observability.context import scoped


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Record safe per-request access data under a fresh correlation scope."""

    def __init__(self, app, *, logger_name: str = "avatar.api.access") -> None:
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next) -> Response:
        started = time.perf_counter()
        with scoped(
            session_id=request.headers.get("x-session-id", "none"),
            request_id=request.headers.get("x-request-id", "none"),
            component="avatar",
        ):
            response = await call_next(request)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            self._logger.info(
                "access method=%s path=%s status=%d latency_ms=%s",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": elapsed_ms,
                },
            )
            return response