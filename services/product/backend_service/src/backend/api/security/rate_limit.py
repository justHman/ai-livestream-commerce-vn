"""backend.api.security.rate_limit — REST, WebSocket, and session rate limiting.

Covers:
  - REST requests (viewer/admin scopes).
  - WebSocket connections + messages (per-connection and per-session budgets).
  - Session activity.

Key design:
  - Keys are ``host:scope:session_id`` — safe, bounded, no customer data.
  - Memory is bounded by ``max_keys`` with LRU-style eviction.
  - Clock is injectable for deterministic tests.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket

from core.api.limits import SlidingWindowLimiter, WebSocketLimiters

_WS_RATE_LIMIT_CLOSE_CODE = 1008


def _request_limit_key(request: Request, scope: str, session_id: str = "") -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}:{scope}:{session_id}"


async def _request_session_id(request: Request) -> str:
    session_id = request.path_params.get("session_id", "")
    if session_id:
        return session_id
    try:
        body = await request.json()
    except (ValueError, Exception):
        return ""
    return str(body.get("session_id", "")) if isinstance(body, dict) else ""


def _limiters_from_request(request: Request) -> SlidingWindowLimiter:
    limiter = getattr(request.app.state, "api_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=500, detail="rate limiter not configured")
    return limiter


async def rate_limit_viewer(request: Request) -> None:
    limiter = _limiters_from_request(request)
    session_id = await _request_session_id(request)
    if not limiter.allow(_request_limit_key(request, "viewer", session_id)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


async def rate_limit_admin(request: Request) -> None:
    limiter = _limiters_from_request(request)
    if not limiter.allow(_request_limit_key(request, "admin")):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def _ws_limit_keys(
    ws: WebSocket, scope: str, session_id: str, connection_id: str
) -> tuple[str, str]:
    host = ws.client.host if ws.client is not None else "unknown"
    session_key = f"{host}:{scope}:{session_id}"
    return f"{session_key}:{connection_id}", session_key


def _ws_limiters(ws: WebSocket) -> WebSocketLimiters:
    limiter = getattr(ws.app.state, "ws_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=500, detail="ws limiter not configured")
    return limiter


async def allow_ws_message(
    ws: WebSocket, scope: str, session_id: str, connection_id: str
) -> bool:
    """Apply the per-connection then per-session budgets; close on exceed."""
    connection_key, session_key = _ws_limit_keys(ws, scope, session_id, connection_id)
    allowed = _ws_limiters(ws).allow(connection_key, session_key)
    if not allowed:
        await ws.close(code=_WS_RATE_LIMIT_CLOSE_CODE, reason="message rate limit exceeded")
    return allowed