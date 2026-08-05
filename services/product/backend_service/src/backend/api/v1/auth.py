"""backend.api.v1.auth — token-based auth dependencies (Task 1.25 copy).

Copied from ``core/api/auth.py`` (COPY-DON'T-IMPORT) so the canonical backend
service is self-contained. Config is read from the request-scoped
``BootstrapContainer`` instead of the legacy ``v1.deps()`` singleton.

Two auth planes:
  - VIEWER (BACKEND_API_TOKEN): /sessions/* + /ws/control/{session_id}.
  - ADMIN (ADMIN_API_TOKEN):   /engines/* + /admin/*.

Rules:
  - APP_ENV="dev" + token empty  -> auth DISABLED (dev/local + existing tests).
  - APP_ENV="prod" + token empty -> every request to that plane is 401.
  - Missing Authorization header   -> 401.
  - Wrong token                    -> 401 (do not leak valid-vs-invalid).
  - Viewer token on admin endpoint -> 403.

WebSocket auth:
  - ``validate_ws_token(ws, config)`` checks ``ws.query_params["token"]``.
  - Returns True if valid OR if dev+no-token. The route MUST call this BEFORE
    ``await ws.accept()``; on False, ``await ws.close(code=4401)`` and return.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, WebSocket

from backend.api.dependencies import container_from_request
from backend.config import AppConfig


# ── constant-time token compare ─────────────────────────────────────


def _tokens_match(a: str, b: str) -> bool:
    """Constant-time comparison for two non-empty token strings."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── helpers ─────────────────────────────────────────────────────────


def _parse_bearer(header_value: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header.

    Returns None if the header is missing or malformed. We do NOT raise here
    — the caller decides 401 (missing) vs 401 (wrong) without leaking which.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _auth_disabled_dev(cfg: AppConfig, token: str) -> bool:
    """In dev with no token configured, auth is disabled."""
    return cfg.app_env == "dev" and token == ""


def _cfg(request: Request) -> AppConfig | None:
    try:
        return container_from_request(request).config
    except RuntimeError:
        return None  # no container wired — do not block


# ── HTTP dependencies ───────────────────────────────────────────────


async def viewer_auth(request: Request) -> None:
    """FastAPI dependency: validate BACKEND_API_TOKEN for viewer routes.

    - dev + empty token -> pass (auth disabled).
    - prod + empty token -> 401 (viewer auth required, no bypass).
    - missing/wrong token -> 401.
    """
    cfg = _cfg(request)
    if cfg is None:
        return  # no config wired — do not block
    token = cfg.backend_api_token
    if _auth_disabled_dev(cfg, token):
        return
    presented = _parse_bearer(request.headers.get("authorization"))
    if not token:
        raise HTTPException(status_code=401, detail="viewer auth required")
    if presented is None or not _tokens_match(presented, token):
        raise HTTPException(status_code=401, detail="invalid credentials")


async def admin_auth(request: Request) -> None:
    """FastAPI dependency: validate ADMIN_API_TOKEN for admin routes.

    - dev + empty admin token -> pass (auth disabled).
    - prod + empty admin token -> 401.
    - missing -> 401.
    - wrong -> 401.
    - valid viewer token (matches backend_api_token but not admin) -> 403.
    - valid admin token -> pass.
    """
    cfg = _cfg(request)
    if cfg is None:
        return
    admin_token = cfg.admin_api_token
    if _auth_disabled_dev(cfg, admin_token):
        return
    presented = _parse_bearer(request.headers.get("authorization"))
    if not admin_token:
        raise HTTPException(status_code=401, detail="admin auth required")
    if presented is not None and _tokens_match(presented, admin_token):
        return
    if presented and cfg.backend_api_token and _tokens_match(presented, cfg.backend_api_token):
        raise HTTPException(status_code=403, detail="admin privilege required")
    raise HTTPException(status_code=401, detail="invalid credentials")


# ── WebSocket auth ──────────────────────────────────────────────────


def validate_ws_token(ws: WebSocket, config: AppConfig) -> bool:
    """Validate the WS token from ``?token=...`` before ``ws.accept()``.

    Returns True if:
      - the query token matches ``config.backend_api_token``, OR
      - dev mode + no configured token (auth disabled).

    Returns False otherwise (missing/wrong token in prod). The caller MUST
    ``await ws.close(code=4401)`` and return WITHOUT accepting on False.
    """
    token = config.backend_api_token
    if _auth_disabled_dev(config, token):
        return True
    if not token:
        return False
    presented = ws.query_params.get("token")
    if presented is None:
        return False
    return _tokens_match(presented, token)


__all__ = [
    "viewer_auth",
    "admin_auth",
    "validate_ws_token",
]
