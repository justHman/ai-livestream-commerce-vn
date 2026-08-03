"""API dependencies: expose the active TTS engine and concurrency gates."""

from __future__ import annotations

from fastapi import Request

from tts.api.security.authentication import get_security_config
from tts.api.security.rate_limit import ConcurrencyLimiter, GPUConcurrencyLimiter
from tts.config import ServerConfig
from tts.engines.base import EngineUnavailable, TTSEngine


def get_engine(request: Request) -> TTSEngine:
    """Return the active self-host TTS engine owned by the lifespan."""
    engine: TTSEngine | None = getattr(request.app.state, "engine", None)
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


def create_dependency_overrides(
    server: ServerConfig, security: object
) -> dict:
    """Wire overrides so tests can inject fakes for engine/limits."""
    return {}