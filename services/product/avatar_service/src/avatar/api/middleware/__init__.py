"""Middleware package for the avatar self-host service."""

from avatar.api.middleware.access_log import AccessLogMiddleware
from avatar.api.middleware.body_limit import BodyLimitMiddleware
from avatar.api.middleware.security_headers import SecurityHeadersMiddleware
from avatar.api.middleware.registry import register_middleware

__all__ = [
    "AccessLogMiddleware",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "register_middleware",
]