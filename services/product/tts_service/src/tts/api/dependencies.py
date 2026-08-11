"""API dependencies: engine, voice service, and tenant resolution."""

from __future__ import annotations

from fastapi import Request

from tts.api.security.authentication import get_security_config
from tts.api.security.rate_limit import ConcurrencyLimiter, GPUConcurrencyLimiter
from tts.config import ServerConfig
from tts.engines.base import EngineUnavailable, TTSEngine
from tts.voices.service import VoiceProfileService


def get_engine(request: Request) -> TTSEngine:
    """Return the active self-host TTS engine owned by the lifespan."""
    engine: TTSEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise EngineUnavailable("engine not started")
    return engine


def get_tenant_id(request: Request) -> str:
    """Resolve the tenant from the ``X-Tenant-Id`` header (default "default")."""
    tenant = request.headers.get("x-tenant-id")
    return tenant.strip() if tenant and tenant.strip() else "default"


def get_voice_service(request: Request) -> VoiceProfileService:
    """Return the voice-profile service owned by the app state.

    Raises 503 when the runtime has not wired the service yet — the same
    boundary the provider readiness gate uses (cluster 4 wires the provider).
    """
    service: VoiceProfileService | None = getattr(request.app.state, "voice_service", None)
    if service is None:
        from tts.providers.errors import ProviderUnavailableError

        raise ProviderUnavailableError("voice service not started")
    return service


def get_concurrency_limiter(request: Request) -> ConcurrencyLimiter:
    config = get_security_config(request)
    return ConcurrencyLimiter(max_concurrent=config.max_concurrent_requests)


def get_gpu_concurrency_limiter(request: Request) -> GPUConcurrencyLimiter:
    config = get_security_config(request)
    return GPUConcurrencyLimiter(max_gpu_concurrent=config.max_gpu_concurrent_requests)


def create_dependency_overrides(server: ServerConfig, security: object) -> dict:
    """Wire overrides so tests can inject fakes for engine/limits."""
    return {}
