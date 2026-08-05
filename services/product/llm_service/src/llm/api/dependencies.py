"""API dependencies: expose the active engine and concurrency gates.

Routes resolve the active engine from these dependencies and invoke the
typed base interface directly (Task 1.31) — no pass-through delegation layer.
"""

from __future__ import annotations

from fastapi import Request

from llm.api.security.authentication import get_security_config
from llm.api.security.rate_limit import ConcurrencyLimiter, GPUConcurrencyLimiter
from llm.config import ServerConfig
from llm.engines.base import EngineUnavailable, LLMEngine


def get_engine(request: Request) -> LLMEngine:
    """Return the active self-host engine owned by the lifespan."""
    engine: LLMEngine | None = getattr(request.app.state, "engine", None)
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
