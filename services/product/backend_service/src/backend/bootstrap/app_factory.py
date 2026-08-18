"""backend.bootstrap.app_factory — FastAPI composition root.

Creates the FastAPI app, registers the supplied lifespan, framework CORS,
access-log/body-limit/security-headers middleware, exception handlers, and
the v1 router, and attaches a typed ``BootstrapContainer`` to ``app.state``.

Canonical dependency access goes through ``app.state.container`` (see
``backend.api.dependencies``); the bootstrap never touches the legacy
``core.api.v1`` process-global seam (``init_deps``/``deps``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import AppConfig

from .container import BootstrapContainer, create_container
from .lifespan import build_lifespan

logger = logging.getLogger(__name__)


def _build_director_pipeline(config, backend, engine_manager):
    """Construct the agentic Director pipeline (P0-01).

    Returns ``(runtime, coordinator, reducer)`` or ``(None, None, None)`` when
    ``DIRECTOR_ENABLED=0`` keeps the pipeline inert. The composition root owns
    construction so the SAME instances are injected into
    ``PlatformEventIngestionService`` and the lifespan's reducer loop.
    """
    if not config.director_enabled:
        return None, None, None
    from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.director.session_context import DirectorRuntime
    from backend.application.reducer import FastReducer

    embedder = HashingEmbedder()  # offline/CI-safe; reducer REQUIRES an embedder
    runtime = DirectorRuntime(backend, embedder=embedder)
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=engine_manager.llm if engine_manager is not None else None,
        tts=engine_manager.tts if engine_manager is not None else None,
        backend=backend,
        cfg=CoordinatorConfig(),
    )
    reducer = FastReducer(embedder=embedder)
    return runtime, coordinator, reducer


def _build_container(config, container: BootstrapContainer | None) -> BootstrapContainer:
    """Construct the container from ``config`` or use the injected one."""
    if container is not None:
        if container.config is None:
            container.config = config
        return container
    backend = config.build_render_backend()
    store = config.build_store()
    engine_manager = v1_engine_manager(config)
    pg_store = _build_pg_store(config)
    script_authoring = _build_script_authoring(config, engine_manager, pg_store)
    from backend.application.platform_events import PlatformEventIngestionService
    from backend.application.publishing import LiveKitPublisherRegistry, publish_enabled

    director, coordinator, reducer = _build_director_pipeline(config, backend, engine_manager)

    return create_container(
        backend=backend,
        store=store,
        config=config,
        engine_manager=engine_manager,
        pg_store=pg_store,
        livekit_publishers=LiveKitPublisherRegistry() if publish_enabled() else None,
        director=director,
        coordinator=coordinator,
        reducer=reducer,
        script_authoring_service=script_authoring,
        event_ingestion=PlatformEventIngestionService(
            store=store,
            pg_store=pg_store,
            coordinator=coordinator,
            runtime=director,
            reducer=reducer,
        ),
    )


def v1_engine_manager(config) -> Any:
    """Build an EngineManager and load configured engines (parity helper)."""
    from backend.engine_manager import EngineManager

    manager = EngineManager()
    try:
        if config.llm.engine not in ("none", "", None):
            manager.load_llm(config.llm.to_engine_cfg())
            manager.set_system_prompt(config.llm.system_prompt)
    except Exception as exc:
        manager.llm_load_error = f"{type(exc).__name__}: {exc}"
        print(
            f"[bootstrap] LLM engine '{config.llm.engine}' unavailable "
            f"({type(exc).__name__}: {exc}); using echo stub."
        )
    try:
        if config.tts.engine not in ("tone", "", None):
            manager.load_tts(config.tts.to_engine_cfg())
    except Exception as exc:
        manager.tts_load_error = f"{type(exc).__name__}: {exc}"
        print(
            f"[bootstrap] TTS engine '{config.tts.engine}' unavailable "
            f"({type(exc).__name__}: {exc}); using tone stub."
        )
    if config.render_backend == "cloud_liveavatar":
        manager.reconfigure_cloud()
    return manager


def _build_pg_store(config) -> Any:
    if not config.database_url:
        return None
    from backend.application.db.postgres_store import PostgresRuntimeStore

    return PostgresRuntimeStore(config.database_url)


def _build_script_authoring(config, engine_manager, pg_store) -> Any:
    """Build the Change B authoring service when Postgres is configured.

    Returns ``ScriptAuthoringServiceImpl | None``. When ``pg_store`` is None
    (no DATABASE_URL) the service stays None so /api/v1/script-sets keeps
    returning 501; the log makes the disabled state explicit. ``engine_manager``
    powers the B6 AI generation commands; when the engine is unavailable the
    four AI commands raise ``llm_unavailable`` (503).
    """
    if pg_store is None:
        logger.info("script authoring disabled (no DATABASE_URL); /api/v1/script-sets stays 501")
        return None
    from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
    from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl

    repos = PostgresAuthoringRepositories(config.database_url)
    return ScriptAuthoringServiceImpl(
        repos, config=config.script_authoring, engine_manager=engine_manager
    )


def _build_api_limiter(config) -> Any:
    from backend.application.render.limiters import SlidingWindowLimiter

    return SlidingWindowLimiter(
        limit=config.api_rate_limit_requests,
        window_seconds=config.api_rate_limit_window_seconds,
        max_keys=config.api_rate_limit_max_keys,
    )


def _build_ws_limiter(config) -> Any:
    from backend.application.render.limiters import WebSocketLimiters

    return WebSocketLimiters(
        limit=config.ws_rate_limit_messages,
        window_seconds=config.ws_rate_limit_window_seconds,
        max_keys=config.api_rate_limit_max_keys,
    )


def _include_router(app: FastAPI) -> None:
    """Mount the canonical v1 router (self-contained copy) + health router.

    ``backend.api.v1`` is the COPY-DON'T-IMPORT route set migrated from
    ``core.api.v1`` (Task 1.25). Health stays outside the versioned contract.
    """
    from backend.api.health import router as health_router
    from backend.api.v1 import router as v1_router

    app.include_router(v1_router)
    app.include_router(health_router)


def _register_middleware(app: FastAPI, config) -> None:
    from backend.api.middleware.access_log import AccessLogMiddleware
    from backend.api.middleware.body_limit import BodyLimitMiddleware
    from backend.api.middleware.security_headers import SecurityHeadersMiddleware

    # Order matters: access log outermost, then security headers, then
    # body limit, then framework CORS closest to routes.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_list(),
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _register_exception_handlers(app: FastAPI) -> None:
    from backend.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)


def create_app(
    config=None,
    deps=None,
    container: BootstrapContainer | None = None,
    *,
    lifespan=None,
) -> FastAPI:
    """Build a fresh, isolated FastAPI backend app.

    Parameters
    ----------
    config:
        Optional ``AppConfig``; defaults to ``AppConfig.from_env()``.
    deps:
        Legacy injected deps object for existing tests (attributes mirror
        ``BootstrapContainer`` fields).  When provided, the app mirrors them
        into the typed container and builds no engines.
    container:
        Optional typed ``BootstrapContainer``.  When provided, it is attached
        to ``app.state``.  No engines/network are touched.
    lifespan:
        Optional explicit asyncio lifespan for the app.
    """
    if config is None:
        config = AppConfig.from_env()

    if config.app_env != "dev" and config.cors_list() == ["*"]:
        raise RuntimeError(
            "CORS_ORIGINS='*' is forbidden outside APP_ENV=dev; set explicit origins"
        )

    # Resolve the typed container (canonical path) or the legacy deps path.
    resolved_container: BootstrapContainer
    if container is not None:
        resolved_container = container
    elif deps is not None:
        from backend.application.entity.repository import InMemoryEntityRepository

        resolved_container = BootstrapContainer(
            backend=deps.backend,
            store=deps.store,
            config=deps.config or config,
            engine_manager=deps.engine_manager,
            director=deps.director,
            coordinator=deps.coordinator,
            pg_store=deps.pg_store,
            livekit_publishers=deps.livekit_publishers,
            hub=deps.hub,
            locks=deps.locks,
            orchestrators=deps.orchestrators,
            avatars=deps.avatars,
            script_authoring_service=getattr(deps, "script_authoring_service", None),
            event_ingestion=getattr(deps, "event_ingestion", None),
            entity_repo=getattr(deps, "entity_repo", None) or InMemoryEntityRepository(),
        )
    else:
        resolved_container = _build_container(config, container=None)

    app_lifespan = lifespan if lifespan is not None else build_lifespan(resolved_container)
    app = FastAPI(title="VN Live-Commerce Host — core API", lifespan=app_lifespan)

    app.state.container = resolved_container
    app.state.config = resolved_container.config
    app.state.api_limiter = _build_api_limiter(config)
    app.state.ws_limiter = _build_ws_limiter(config)

    _register_middleware(app, config)
    _register_exception_handlers(app)
    _include_router(app)

    @app.get("/")
    async def root() -> dict:
        engine_manager = resolved_container.engine_manager
        backend = resolved_container.backend
        llm_name = (
            engine_manager.llm.name
            if engine_manager is not None and engine_manager.llm
            else "none(stub)"
        )
        tts_name = (
            engine_manager.tts.name
            if engine_manager is not None and engine_manager.tts
            else "tone(stub)"
        )
        return {
            "service": "vn-live-commerce-host",
            "api": "/api/v1",
            "render_backend": backend.name if backend is not None else None,
            "llm_engine": llm_name,
            "tts_engine": tts_name,
            "director_enabled": resolved_container.director is not None,
            "engine_manager": engine_manager is not None,
        }

    return app


__all__ = ["create_app"]
