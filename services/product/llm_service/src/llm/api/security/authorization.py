"""Scope-based authorization for the LLM service.

Preserves the 401 unauthenticated vs 403 unauthorized distinction. A
service token grants the `llm.inference` and `llm.models` scopes; an admin
token grants all scopes.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from llm.api.security.authentication import get_auth_subject

SERVICE_SCOPES = frozenset({"llm.inference", "llm.models"})
ADMIN_SCOPES = frozenset({"llm.admin", "llm.contracts", "llm.health"})


class AuthorizationError(HTTPException):
    """Raised when authenticated but not authorized for a scope."""

    def __init__(self, detail: str = "insufficient scope") -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def validate_scope(subject: str, scope: str) -> None:
    """Raise 403 when the authenticated subject lacks the required scope."""
    if subject == "admin":
        granted = ADMIN_SCOPES | SERVICE_SCOPES
    elif subject == "service":
        granted = SERVICE_SCOPES
    else:
        granted = frozenset()
    if scope not in granted:
        raise AuthorizationError(f"missing required scope: {scope}")


def require_scope(scope: str):
    """Dependency factory: authenticate then authorize for `scope`."""

    def dependency(subject: str = Depends(get_auth_subject)) -> str:
        validate_scope(subject, scope)
        return subject

    return dependency