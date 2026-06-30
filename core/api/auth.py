"""core.api.auth — token-based auth dependencies (Task 7).

Two auth planes:
  - VIEWER (BACKEND_API_TOKEN): /lite/* + /ws/control/{session_id}.
  - ADMIN (ADMIN_API_TOKEN):   /engines/* + /debug/*.

Rules:
  - APP_ENV="dev" + token empty  -> auth DISABLED (dev/local + existing tests).
  - APP_ENV="prod" + token empty -> every request to that plane is 401
    (admin auth required in prod; no bypass).
  - Missing Authorization header   -> 401.
  - Wrong token                    -> 401 (do not leak valid-vs-invalid).
  - Viewer token on admin endpoint -> 403 (a valid viewer token is a known
    principal but lacks admin privilege — distinguish from "no/wrong token").

WebSocket auth:
  - ``validate_ws_token(ws, config)`` checks ``ws.query_params["token"]``
    (simplest for WS clients: ``ws://host/api/v1/ws/control/sid?token=...``).
  - Returns True if valid OR if dev+no-token. The route MUST call this BEFORE
    ``await ws.accept()``; on False, ``await ws.close(code=4401)`` and return
    (do NOT accept then close).
"""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket

from ..config import AppConfig


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


# ── HTTP dependencies ───────────────────────────────────────────────


async def viewer_auth(request: Request) -> None:
    """FastAPI dependency: validate BACKEND_API_TOKEN for /lite/* routes.

    - dev + empty token -> pass (auth disabled).
    - prod + empty token -> 401 (viewer auth required, no bypass).
    - missing/wrong token -> 401.
    """
    from . import v1

    cfg = v1.deps().config
    if cfg is None:
        return  # no config wired (pre-Task-7 path) — do not block
    token = cfg.backend_api_token
    if _auth_disabled_dev(cfg, token):
        return
    presented = _parse_bearer(request.headers.get("authorization"))
    if not token:
        # prod with empty configured token: auth required, nothing to match.
        raise HTTPException(status_code=401, detail="viewer auth required")
    if presented != token:
        raise HTTPException(status_code=401, detail="invalid credentials")


async def admin_auth(request: Request) -> None:
    """FastAPI dependency: validate ADMIN_API_TOKEN for /engines/* + /debug/*.

    - dev + empty admin token -> pass (auth disabled).
    - prod + empty admin token -> 401 (admin auth required, no bypass).
    - missing -> 401.
    - wrong -> 401.
    - valid viewer token (matches backend_api_token but not admin) -> 403.
    - valid admin token -> pass.
    """
    from . import v1

    cfg = v1.deps().config
    if cfg is None:
        return  # no config wired (pre-Task-7 path) — do not block
    admin_token = cfg.admin_api_token
    if _auth_disabled_dev(cfg, admin_token):
        return
    presented = _parse_bearer(request.headers.get("authorization"))
    if not admin_token:
        # prod with empty admin token: admin auth required, nothing to match.
        raise HTTPException(status_code=401, detail="admin auth required")
    if presented == admin_token:
        return
    # Distinguish a valid-but-insufficient viewer token from a wrong token.
    if presented and presented == cfg.backend_api_token and cfg.backend_api_token:
        raise HTTPException(status_code=403, detail="admin privilege required")
    raise HTTPException(status_code=401, detail="invalid credentials")


async def debug_enabled_dep() -> None:
    """FastAPI dependency: 404 if DEBUG_ENABLED is False.

    Runs BEFORE admin_auth on /debug/* so that disabled-debug endpoints 404
    regardless of credentials (no existence leak to unauthenticated probes).
    """
    from . import v1

    cfg = v1.deps().config
    if cfg is None:
        return  # no config wired — do not block (pre-Task-7 path)
    if not cfg.debug_enabled:
        raise HTTPException(status_code=404, detail="not found")


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
        # prod with empty configured token: auth required, nothing to match.
        return False
    presented = ws.query_params.get("token")
    if presented is None:
        return False
    return presented == token


__all__ = [
    "viewer_auth",
    "admin_auth",
    "debug_enabled_dep",
    "validate_ws_token",
]
