"""Constant-time bearer authentication for the avatar service."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from avatar.api.security.config import SecurityConfig


class AuthenticationError(HTTPException):
    """Raised when request authentication fails."""

    def __init__(self, detail: str = "invalid or missing credentials") -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _verify_token(provided: str, expected: str) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def get_security_config(request: Request) -> SecurityConfig:
    return getattr(request.app.state, "security_config", SecurityConfig())


def authenticate_bearer(
    config: SecurityConfig,
    authorization: str | None,
) -> str:
    """Validate the bearer token, returning the authenticated scope subject."""
    if not config.auth_enabled:
        return "service"  # auth disabled = trusted internal runtime
    if not authorization:
        raise AuthenticationError("missing Authorization header")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise AuthenticationError("invalid authorization scheme")
    if _verify_token(credential, config.auth_token):
        return "service"
    if config.admin_token and _verify_token(credential, config.admin_token):
        return "admin"
    raise AuthenticationError("invalid credentials")


def get_auth_subject(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: authenticate against the app security config."""
    return authenticate_bearer(get_security_config(request), authorization)