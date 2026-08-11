"""TTS engine lifecycle — bounded startup/shutdown in the ASGI lifespan.

Provider wiring (Change T cluster 4): the provider starts when the runtime
config selects it and the SDK is importable. Startup failure never crashes
the app — the legacy engine keeps serving and the provider stays gated
behind ``runtime_ready`` so routes raise 503 until it is genuinely up.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tts.engines.base import TTSEngine, load_engine

logger = logging.getLogger("tts.bootstrap.lifespan")


def _build_engine() -> TTSEngine:
    """Construct the active self-host TTS engine from validated config."""
    from tts.config import load_engine_config

    cfg = load_engine_config()
    if cfg.engine == "none":
        return load_engine({})  # deterministic tone engine
    return load_engine(cfg.to_cfg_dict())


def _build_provider(app: FastAPI):
    """Construct the runtime provider, or None when not configured/ready.

    The provider is wired from the runtime config + voice service. Every
    failure path returns None with a clear log line — readiness stays false
    and the legacy engine keeps serving.
    """
    from tts.providers.errors import ProviderError
    from tts.providers.vieneu_v3 import VieNeuV3TurboProvider

    runtime = app.state.runtime_config
    if runtime.provider == "none":
        return None
    voice_service = app.state.voice_service

    def profile_loader(voice_profile_id: str, tenant_id: str):
        return voice_service.get_profile_payload(voice_profile_id, tenant_id)

    try:
        provider = VieNeuV3TurboProvider(runtime, profile_loader=profile_loader)
    except ProviderError as exc:
        logger.error(
            "TTS provider startup failed (%s); legacy engine keeps serving, "
            "provider gated until runtime_ready",
            exc,
        )
        return None
    logger.info(
        "TTS provider ready provider=%s backend=%s model_revision=%s",
        provider.provider_name,
        provider.backend,
        runtime.model_revision,
    )
    return provider


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run one configured TTS engine, bounded to the application lifetime."""
    engine = _build_engine()
    app.state.engine = engine
    app.state.engine_ready = True
    # Voice-profile service over the configured store URI (runtime cluster
    # replaces the injected enrollment fn with the real provider).
    _wire_voice_service(app)
    # Provider-owned lifecycle: starts when config selects it; failure only
    # flips runtime_ready, never crashes the app. With no provider configured
    # ("none") the legacy engine alone satisfies runtime readiness.
    provider = _build_provider(app)
    app.state.provider = provider
    app.state.runtime_ready = provider is not None or app.state.runtime_config.provider == "none"
    try:
        yield {"engine": engine}
    finally:
        app.state.engine_ready = False
        app.state.runtime_ready = False
        app.state.voice_service = None
        try:
            engine.unload()
        finally:
            app.state.engine = None


def _wire_voice_service(app: FastAPI) -> None:
    """Build the VoiceProfileService from the app's validated config."""
    from tts.voices.service import VoiceProfileService
    from tts.voices.store import get_store

    server = app.state.server_config
    runtime = app.state.runtime_config
    store = get_store(runtime.voice_store_uri, server.runtime_root)
    app.state.voice_service = VoiceProfileService(store, runtime)
