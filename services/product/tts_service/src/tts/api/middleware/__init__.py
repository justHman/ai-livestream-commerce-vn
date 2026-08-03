"""Middleware package for the TTS self-host service."""

from tts.api.middleware.access_log import AccessLogMiddleware
from tts.api.middleware.body_limit import BodyLimitMiddleware
from tts.api.middleware.security_headers import SecurityHeadersMiddleware
from tts.api.middleware.registry import register_middleware

__all__ = [
    "AccessLogMiddleware",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "register_middleware",
]