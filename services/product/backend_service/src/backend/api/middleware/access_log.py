"""backend.api.middleware.access_log — safe access records with guaranteed cleanup.

Binds validated correlation identifiers from safe request headers into the
observability context, emits method/path/status/latency access records with
NO query string, tokens, body, or customer data, and always restores the
previous context in ``finally`` (success, HTTPException, unexpected error,
and cancellation alike).

The context package is deliberately kept local to ``backend.api`` (no deep
import into ``services/...``): the access record contract must not depend on
the service packaging layout.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)
_ContextVar = ContextVar  # re-export for type annotation clarity

_CONTEXT_FIELDS = ("session_id", "request_id", "trace_id", "component")
_SAFE_HEADERS = {
    "x-request-id": "request_id",
    "x-trace-id": "trace_id",
    "x-session-id": "session_id",
    "x-component": "component",
}
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", re.ASCII)
_context_vars = {
    field: ContextVar(f"api_access_{field}", default=None) for field in _CONTEXT_FIELDS
}


def _bind_context(identifiers: dict[str, str]) -> list[tuple[ContextVar, Token]]:
    """Bind validated identifiers, returning tokens for restoration."""
    bound: list[tuple[ContextVar, Token]] = []
    for field, value in identifiers.items():
        if _IDENTIFIER_PATTERN.fullmatch(value):
            bound.append((_context_vars[field], _context_vars[field].set(value)))
    return bound


def _reset_context(bound: list[tuple[ContextVar, Token]]) -> None:
    for variable, token in reversed(bound):
        variable.reset(token)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Bind correlation context, emit one safe access record per request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        context: dict[str, str] = {}
        for header, field in _SAFE_HEADERS.items():
            value = request.headers.get(header)
            if value and len(value) <= 128:
                context[field] = value
        tokens = _bind_context(context)

        started = time.monotonic()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            status = getattr(response, "status_code", 0)
            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            logger.info(
                "access method=%s path=%s status=%s latency_ms=%s",
                request.method,
                request.url.path,
                status,
                latency_ms,
            )
            _reset_context(tokens)