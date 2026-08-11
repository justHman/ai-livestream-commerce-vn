"""FastAPI application factory for the TTS self-host service."""

from __future__ import annotations

from fastapi import FastAPI

from tts.api import exception_handlers
from tts.api.dependencies import create_dependency_overrides
from tts.api.health import router as health_router
from tts.api.middleware import register_middleware
from tts.api.security.config import SecurityConfig
from tts.api.v1.router import router as v1_router
from tts.bootstrap.lifespan import create_lifespan
from tts.config import ServerConfig


def create_app(
    server: ServerConfig | None = None,
    *,
    security: SecurityConfig | None = None,
) -> FastAPI:
    """Build and return a configured FastAPI application."""
    from tts.config import load_security_config, load_server_config

    cfg = server or load_server_config()
    sec = security if security is not None else load_security_config()

    app = FastAPI(
        title="ai-live-tts",
        version="0.1.0",
        description="Self-host TTS synthesis service",
        lifespan=create_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.server_config = cfg
    app.state.security_config = sec
    app.state.engine = None
    app.state.engine_ready = False
    # Runtime subsystem readiness (provider/voice store/scheduler) — the
    # runtime cluster flips this after provider startup; compatibility flag
    # `engine_ready` alone no longer means /ready.
    app.state.runtime_ready = False

    register_middleware(app, cfg)
    exception_handlers.register(app)
    app.include_router(health_router, tags=["health"])
    app.include_router(v1_router, prefix="/v1")

    app.dependency_overrides.update(create_dependency_overrides(cfg, sec))

    return app
