"""FastAPI application factory for the LLM self-host service.

No container abstraction: one active engine owns the process. Routes resolve
the active engine through a dependency (api/dependencies.py) and invoke the
typed base interface directly.
"""

from __future__ import annotations

from fastapi import FastAPI

from llm.api import exception_handlers
from llm.api.dependencies import create_dependency_overrides
from llm.api.health import router as health_router
from llm.api.middleware import register_middleware
from llm.api.security.config import SecurityConfig
from llm.api.v1.router import router as v1_router
from llm.bootstrap.lifespan import create_lifespan
from llm.config import ServerConfig


def create_app(
    server: ServerConfig | None = None,
    *,
    security: SecurityConfig | None = None,
) -> FastAPI:
    """Build and return a configured FastAPI application."""
    from llm.config import load_security_config, load_server_config

    cfg = server or load_server_config()
    sec = security if security is not None else load_security_config()

    app = FastAPI(
        title="ai-live-llm",
        version="0.1.0",
        description="Self-host LLM inference service",
        lifespan=create_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # served via the contracts endpoint
    )

    app.state.server_config = cfg
    app.state.security_config = sec
    app.state.engine = None
    app.state.engine_ready = False

    register_middleware(app, cfg)
    exception_handlers.register(app)
    app.include_router(health_router, tags=["health"])
    app.include_router(v1_router, prefix="/v1")

    app.dependency_overrides.update(create_dependency_overrides(cfg, sec))

    return app
