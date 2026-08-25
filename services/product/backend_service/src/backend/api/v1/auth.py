"""backend.api.v1.auth — token-based auth dependencies.

Route modules import ``viewer_auth`` / ``admin_auth`` / ``validate_ws_token``
from here so the v1 surface shares ONE canonical token-validation truth: the
fail-closed implementation in ``backend.api.security.authentication``
(R8.9). Unresolved container/auth config denies with 401 instead of letting
the request through.

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

from backend.api.security.authentication import (
    require_admin as admin_auth,
    require_viewer as viewer_auth,
    ws_token_valid as validate_ws_token,
)

__all__ = [
    "viewer_auth",
    "admin_auth",
    "validate_ws_token",
]
