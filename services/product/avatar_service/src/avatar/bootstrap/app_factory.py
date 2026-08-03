"""FastAPI application factory for the avatar self-host service."""

from __future__ import annotations

from fastapi import FastAPI

from avatar.api import exception_handlers
from avatar.api.dependencies import create_dependency_overrides
from avatar.api.health import router as health_router
from avatar.api.middleware import register_middleware
from avatar.api.security.config import SecurityConfig
from avatar.api.v1.router import router as v1_router
from avatar.bootstrap.lifespan import create_lifespan
from avatar.config import ServerConfig


def create_app(
    server: ServerConfig | None = None,
    *,
    security: SecurityConfig | None = None,
) -> FastAPI:
    """Build and return a configured FastAPI application."""
    from avatar.config import load_security_config, load_server_config

    cfg = server or load_server_config()
    sec = security if security is not None else load_security_config()

    app = FastAPI(
        title="ai-live-avatar",
        version="0.1.0",
        description="Self-host avatar rendering service",
        lifespan=create_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.server_config = cfg
    app.state.security_config = sec
    app.state.container = None
    app.state.engine = None
    app.state.engine_ready = False

    register_middleware(app, cfg)
    exception_handlers.register(app)
    app.include_router(health_router, tags=["health"])
    app.include_router(v1_router, prefix="/v1")

    app.dependency_overrides.update(create_dependency_overrides(cfg, sec))

    return app