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


def _build_runtime(app: FastAPI, provider) -> object | None:
    """Construct the scheduler runtime over the ready provider (task 10.1).

    One lane per provider — Change T has exactly one provider, so a single
    ``SchedulerRuntime`` is sufficient; a future multi-provider deployment
    runs one runtime per provider lane.
    """
    from tts.scheduler.admission import AdmissionController
    from tts.scheduler.fairness import FairnessSelector, PendingPopulation
    from tts.scheduler.runtime import SchedulerRuntime

    if provider is None:
        return None
    cfg = app.state.runtime_config
    # P1-07: pre-admission validation from the provider. Unsupported client
    # input (style/cue/format) raises a typed 4xx ProviderError BEFORE any
    # capacity is consumed, so one invalid request can never fail an entire
    # provider batch of unrelated siblings.
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(
            cfg.global_pending_limit,
            cfg.per_session_pending_limit,
            validate=provider.validate_request,
        ),
        selector=FairnessSelector(),
        provider=provider,
        config=cfg,
        metrics=app.state.metrics,
    )


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run one configured TTS engine, bounded to the application lifetime."""
    engine = _build_engine()
    app.state.engine = engine
    app.state.engine_ready = True
    # Process metrics registry (tasks 12.1-12.6) — always wired; endpoints
    # expose its JSON snapshot even when the provider is not up.
    from tts.observability.metrics import get_metrics_registry

    metrics = get_metrics_registry()
    app.state.metrics = metrics
    _record_gpu_metrics(metrics)
    # Voice-profile service over the configured store URI (runtime cluster
    # replaces the injected enrollment fn with the real provider).
    _wire_voice_service(app)
    # Provider-owned lifecycle: starts when config selects it; failure only
    # flips runtime_ready, never crashes the app. With no provider configured
    # ("none") the legacy engine alone satisfies runtime readiness.
    provider = _build_provider(app)
    app.state.provider = provider
    runtime = _build_runtime(app, provider)
    app.state.runtime = runtime
    app.state.runtime_ready = provider is not None or app.state.runtime_config.provider == "none"
    _log_runtime_startup(app, provider, runtime)
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
        if runtime is not None:
            await runtime.close()
            app.state.runtime = None


def _record_gpu_metrics(metrics) -> None:
    """Optionally record GPU/VRAM gauges (task 12.6); never breaks startup.

    torch is not a base dependency — absence means no GPU metrics, which is
    fine for CPU-only and ONNX deployments. Any failure inside this probe
    (no CUDA runtime, driver mismatch) is swallowed: synthesis must not
    depend on metrics.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return
        device_count = torch.cuda.device_count()
        metrics.gauge("gpu_device_count", device_count)
        for index in range(device_count):
            total = torch.cuda.get_device_properties(index).total_memory
            allocated = torch.cuda.memory_allocated(index)
            metrics.gauge(f"gpu_memory_total_bytes_{index}", total)
            metrics.gauge(f"gpu_memory_allocated_bytes_{index}", allocated)
            metrics.gauge(
                f"gpu_memory_utilization_{index}",
                allocated / total if total else 0.0,
            )
    except Exception:
        logger.info("GPU metrics unavailable; skipping", exc_info=True)


def _log_runtime_startup(app: FastAPI, provider, runtime) -> None:
    """Startup log with provider/model/backend + scheduler limits (12.5)."""
    cfg = app.state.runtime_config
    backend = getattr(provider, "backend", "none")
    ready = bool(app.state.runtime_ready)
    logger.info(
        "tts runtime provider=%s model=%s backend=%s accelerator=%s ready=%s "
        "limits={global_pending=%d, per_session_pending=%d, deadline_ms=%d, "
        "max_batch_size=%d, coalesce_window_ms=%d, aging_threshold_ms=%d}",
        cfg.provider,
        cfg.model_revision,
        backend,
        cfg.accelerator,
        ready,
        cfg.global_pending_limit,
        cfg.per_session_pending_limit,
        cfg.request_deadline_ms,
        cfg.max_batch_size,
        cfg.coalesce_window_ms,
        cfg.aging_threshold_ms,
    )


def _wire_voice_service(app: FastAPI) -> None:
    """Build the VoiceProfileService from the app's validated config."""
    from tts.voices.cache import CachedVoiceProfileStore
    from tts.voices.service import VoiceProfileService
    from tts.voices.store import get_store

    server = app.state.server_config
    runtime = app.state.runtime_config
    store = get_store(runtime.voice_store_uri, server.runtime_root)
    cached = CachedVoiceProfileStore(store, maxsize=256, metrics=app.state.metrics)
    app.state.voice_service = VoiceProfileService(cached, runtime, metrics=app.state.metrics)
