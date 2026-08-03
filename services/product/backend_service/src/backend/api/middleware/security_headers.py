"""backend.api.middleware.security_headers — response hardening.

Applies a baseline of security headers to normal AND error responses.
Deliberately does not weaken framework CORS: ``access-control-*`` response
headers already emitted by ``CORSMiddleware`` are left untouched, and the
preflight OPTIONS path is passed through so CORS handling stays in the
framework middleware.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_DEFAULT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security headers; never override existing values."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in _DEFAULT_HEADERS.items():
            if name not in response.headers:
                response.headers[name] = value
        return response