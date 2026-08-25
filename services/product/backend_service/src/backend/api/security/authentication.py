"""backend.api.security.authentication — bearer + WebSocket credential verification.

Verifies ``Authorization: Bearer <token>`` for HTTP dependencies and
``?token=`` for WebSockets.  Secret equality uses constant-time comparison
(``hmac.compare_digest``).  Identity resolution stays safe: no token value is
ever logged or echoed.  Protected HTTP dependencies FAIL CLOSED: when the
container/auth config cannot be resolved, ``require_viewer``/``require_admin``
raise 401 instead of letting the request through.  WebSocket rejection happens
BEFORE ``accept()`` — the route must call ``ws_token_valid``/``reject_ws``
before ``await ws.accept()``.
"""

from __future__ import annotations

import asyncio
import hmac

from fastapi import HTTPException, Request, WebSocket

from backend.config import AppConfig

_WS_AUTH_CLOSE_CODE = 4401


def tokens_match(a: str, b: str) -> bool:
    """Constant-time comparison of two non-empty UTF-8 token strings."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def parse_bearer(header_value: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def auth_disabled_dev(cfg: AppConfig, token: str) -> bool:
    """dev mode with an empty configured token disables auth for this plane."""
    return cfg.app_env == "dev" and token == ""


async def require_viewer(request: Request) -> None:
    """Viewer (BACKEND_API_TOKEN) auth dependency.

    - dev + empty token -> pass.
    - prod + empty configured token -> 401 (auth required, nothing to match).
    - missing/wrong bearer -> 401 (does not leak valid-vs-invalid).
    - unresolved container/config -> 401 (fail closed, never allow).
    """
    cfg = getattr(getattr(request.app.state, "container", None), "config", None)
    if cfg is None:
        raise HTTPException(status_code=401, detail="auth configuration unavailable")
    token = cfg.backend_api_token
    if auth_disabled_dev(cfg, token):
        return
    presented = parse_bearer(request.headers.get("authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="viewer auth required")
    if presented is None or not tokens_match(presented, token):
        raise HTTPException(status_code=401, detail="invalid credentials")


async def require_admin(request: Request) -> None:
    """Admin (ADMIN_API_TOKEN) auth dependency, preserving 401 vs 403.

    401: missing/wrong credentials.
    403: presented token is a valid viewer token but lacks admin privilege.
    Unresolved container/config -> 401 (fail closed, never allow).
    """
    cfg = getattr(getattr(request.app.state, "container", None), "config", None)
    if cfg is None:
        raise HTTPException(status_code=401, detail="auth configuration unavailable")
    admin_token = cfg.admin_api_token
    if auth_disabled_dev(cfg, admin_token):
        return
    presented = parse_bearer(request.headers.get("authorization"))
    if not admin_token:
        raise HTTPException(status_code=401, detail="admin auth required")
    if presented is not None and tokens_match(presented, admin_token):
        return
    if presented and cfg.backend_api_token and tokens_match(presented, cfg.backend_api_token):
        raise HTTPException(status_code=403, detail="admin privilege required")
    raise HTTPException(status_code=401, detail="invalid credentials")


def ws_token_valid(ws: WebSocket, config: AppConfig) -> bool:
    """Validate the WS token from ``?token=...`` before ``accept()``.

    Returns True when the query token matches ``backend_api_token`` or when
    dev mode has no configured token (auth disabled).  The caller MUST
    ``reject_ws(ws)`` and return without accepting on False.
    """
    token = config.backend_api_token
    if auth_disabled_dev(config, token):
        return True
    if not token:
        return False
    presented = ws.query_params.get("token")
    if presented is None:
        return False
    return tokens_match(presented, token)


def reject_ws(ws: WebSocket, code: int = _WS_AUTH_CLOSE_CODE) -> None:
    """Reject an unauthenticated WebSocket BEFORE it is accepted.

    The route calls this and returns without calling ``accept()``, so the
    socket never enters application session/event state.  Starlette buffers
    a ``close()`` issued while still CONNECTING and applies it when the
    endpoint returns, closing the transport with ``code``.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # close() must be awaited; schedule it without blocking the handler.
        loop.create_task(ws.close(code=code))
        return
    # No running loop (should not happen under ASGI); fall back to a
    # synchronous best-effort close.
    asyncio.run(ws.close(code=code))
