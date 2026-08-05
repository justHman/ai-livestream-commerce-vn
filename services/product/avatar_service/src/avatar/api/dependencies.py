"""API dependencies: expose the session manager, engine, and gates."""

from __future__ import annotations

from fastapi import Request

from avatar.api.security.authentication import get_security_config
from avatar.api.security.rate_limit import ConcurrencyLimiter, GPUConcurrencyLimiter
from avatar.config import ServerConfig
from avatar.engines.base import EngineUnavailable, AvatarEngine
from avatar.sessions import SessionManager


def get_sessions(request: Request) -> SessionManager:
    """Return the session lifecycle owner from the container."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise EngineUnavailable("container not started")
    return container.sessions


def get_engine(request: Request) -> AvatarEngine:
    """Return the active self-host avatar engine."""
    engine: AvatarEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise EngineUnavailable("engine not started")
    return engine


def get_server_config(request: Request) -> ServerConfig:
    return getattr(request.app.state, "server_config")


def get_concurrency_limiter(request: Request) -> ConcurrencyLimiter:
    config = get_security_config(request)
    return ConcurrencyLimiter(max_concurrent=config.max_concurrent_requests)


def get_gpu_concurrency_limiter(request: Request) -> GPUConcurrencyLimiter:
    config = get_security_config(request)
    return GPUConcurrencyLimiter(max_gpu_concurrent=config.max_gpu_concurrent_requests)


def create_dependency_overrides(server: ServerConfig, security: object) -> dict:
    """Wire overrides so tests can inject fakes for engine/limits."""
    return {}
