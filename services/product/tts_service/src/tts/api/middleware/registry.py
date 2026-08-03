"""Register all middleware on the TTS FastAPI app."""

from __future__ import annotations

from fastapi import FastAPI

from tts.api.middleware.access_log import AccessLogMiddleware
from tts.api.middleware.body_limit import BodyLimitMiddleware
from tts.api.middleware.security_headers import SecurityHeadersMiddleware
from tts.config import ServerConfig


def register_middleware(app: FastAPI, config: ServerConfig) -> None:
    """Install the middleware stack in reverse execution order."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        BodyLimitMiddleware, max_body_bytes=config.max_body_bytes
    )
    app.add_middleware(AccessLogMiddleware)