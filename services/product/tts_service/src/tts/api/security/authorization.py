"""Scope-based authorization for the TTS service."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from tts.api.security.authentication import get_auth_subject

SERVICE_SCOPES = frozenset({"tts.synthesis", "tts.voices"})
ADMIN_SCOPES = frozenset({"tts.admin", "tts.contracts", "tts.health"})


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
