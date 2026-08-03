"""backend.api.security.authorization — reusable viewer/admin/service scopes.

Preserves the standard distinction:
  401: unauthenticated (missing/invalid credentials).
  403: authenticated but insufficient (valid viewer token on an admin route).

Scopes are thin aliases over the authentication dependencies so route
modules can declare required access without embedding token logic.
"""

from __future__ import annotations

from fastapi import Request

from .authentication import require_admin, require_viewer


async def viewer_scope(request: Request) -> None:
    """Require a valid viewer credential (BACKEND_API_TOKEN)."""
    await require_viewer(request)


async def admin_scope(request: Request) -> None:
    """Require a valid admin credential (ADMIN_API_TOKEN)."""
    await require_admin(request)


async def service_scope(request: Request) -> None:
    """Interceptor-service scope; reserved for authenticated service calls."""


__all__ = ["admin_scope", "service_scope", "viewer_scope"]