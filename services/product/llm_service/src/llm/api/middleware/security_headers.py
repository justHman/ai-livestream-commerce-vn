"""Response hardening middleware: security headers on every response."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set a safe baseline of response hardening headers."""

    HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Cache-Control": "no-store",
    }

    async def dispatch(self, request, call_next) -> Response:
        response = await call_next(request)
        for header, value in self.HEADERS.items():
            response.headers.setdefault(header, value)
        return response
