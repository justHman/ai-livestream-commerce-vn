"""core.server — production FastAPI app for the VN live-commerce host.

Mounts the versioned `/api/v1` router and wires the selected RenderBackend +
SessionStore + LLM/TTS engines from env (see core/config.py). This is the
entrypoint you run on Colab (single process, InMemory store, cloud renderer,
llama.cpp LLM) and lift to AWS (Redis store, vLLM, sticky LB) by changing
ONLY environment variables.

Run:
    uv run uvicorn core.server:app --port 8800
    # or: python -m core.server

Env (key ones — see core/config.py for the full list):
    RENDER_BACKEND=cloud|self_host|mock
    SESSION_STORE=memory|redis
    LLM_ENGINE=vllm|llamacpp|sglang|hf|none
    LLM_MODEL=Qwen/Qwen3-4B-Instruct
    TTS_ENGINE=transformers|vieneu|cosyvoice|tone
    TTS_MODEL=facebook/mms-tts-vie
    DIRECTOR_ENABLED=0|1
    LIVEAVATAR_API_KEY=...           (cloud backend; backend-only secret)

The server auto-builds LLM/TTS engines from env and injects them into the
cloud renderer via configure(). If an engine fails to load (missing deps on a
non-GPU box), it falls back to the built-in stubs so the server still starts,
and records the failure on the EngineManager so /health/ready reports
not-ready (Finding 2). For explicit control (custom cfg), call
core.render.cloud.configure() before serving — the colab launcher does this.

Note (Finding 1): a mock-mode deployment (RENDER_BACKEND=mock) STILL loads
the configured LLM/TTS engines — mock = mock RENDER only; LLM/TTS stay real
per the plan. Only the cloud-specific wiring (the ``cloud`` import +
``reconfigure_cloud()``) is skipped for mock.

App factory:
    create_app(config=None, deps=None) -> FastAPI
      - If ``config`` is None, builds it from env (``AppConfig.from_env()``).
      - If ``deps`` is provided (tests), uses the injected ``V1Deps`` as-is
        and does NOT build/load LLM/TTS engines — the caller owns the deps.
      - Otherwise builds backend/store/hub/engine_manager from ``config``,
        runs the cloud engine-loading block when ``render_backend == "cloud_liveavatar"``
        (identical to the previous module-level wiring), and constructs
        ``V1Deps`` from those built components.

A module-level ``app = create_app()`` is kept at the END of the file for
uvicorn compatibility (``uvicorn core.server:app``). Tests can call
``create_app(config=..., deps=...)`` to get an app without loading models.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import health, v1
from .api.limits import MaxBodySizeMiddleware, SlidingWindowLimiter, WebSocketLimiters
from .config import AppConfig
from .engine_manager import EngineManager
from .livekit_publish import LiveKitPublisherRegistry, publish_enabled
from .render.locks import SessionLockRegistry

# Module-level config — used by ``main()`` for the port and as the default
# for ``create_app()``. Built once at import time from env.
CONFIG = AppConfig.from_env()
logger = logging.getLogger(__name__)
_POSTGRES_STARTUP_ATTEMPTS = 3
_POSTGRES_RETRY_DELAYS = (0.1, 0.2)
_SHUTDOWN_TIMEOUT_SECONDS = 10.0


async def _connect_postgres(pg) -> None:
    for attempt in range(_POSTGRES_STARTUP_ATTEMPTS):
        try:
            await pg.connect()
            await pg.apply_schema()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await pg.close()
            except asyncio.CancelledError:
                raise
            except Exception as close_exc:
                logger.warning(
                    "Postgres cleanup failed after startup attempt=%s error_type=%s",
                    attempt + 1,
                    type(close_exc).__name__,
                )
            if attempt == _POSTGRES_STARTUP_ATTEMPTS - 1:
                logger.error(
                    "Postgres startup failed after %s attempts error_type=%s",
                    _POSTGRES_STARTUP_ATTEMPTS,
                    type(exc).__name__,
                )
                return
            delay = _POSTGRES_RETRY_DELAYS[attempt]
            logger.warning(
                "Postgres startup failed attempt=%s retry_in_seconds=%s", attempt + 1, delay
            )
            await asyncio.sleep(delay)


async def _shutdown(deps: v1.V1Deps, pg) -> None:
    stage = "orchestrators"

    async def run_stage(name: str, operation) -> None:
        nonlocal stage
        stage = name
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Server shutdown cleanup failed stage=%s error_type=%s",
                name,
                type(exc).__name__,
            )

    async def cancel_orchestrators() -> None:
        for session_id, entry in list(deps.orchestrators.items()):
            orchestrator = entry.get("orchestrator")
            if orchestrator is None:
                continue
            try:
                await orchestrator.cancel(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Server shutdown cleanup failed stage=orchestrators error_type=%s",
                    type(exc).__name__,
                )
        deps.orchestrators.clear()

    async def stop_coordinator() -> None:
        if deps.coordinator is not None:
            deps.coordinator.stop_all()

    async def stop_livekit_publishers() -> None:
        if deps.livekit_publishers is not None:
            await deps.livekit_publishers.stop_all()

    async def stop_backend() -> None:
        await asyncio.to_thread(deps.backend.stop_all)

    async def close_backend() -> None:
        close = getattr(deps.backend, "close", None)
        if close is None:
            return
        if inspect.iscoroutinefunction(close):
            await close()
            return
        result = await asyncio.to_thread(close)
        if inspect.isawaitable(result):
            await result

    async def close_postgres() -> None:
        if pg is not None and getattr(pg, "enabled", False):
            await pg.close()

    async def cleanup() -> None:
        await run_stage("orchestrators", cancel_orchestrators)
        await run_stage("coordinator", stop_coordinator)
        await run_stage("livekit.stop_all", stop_livekit_publishers)
        await run_stage("render.stop_all", stop_backend)
        await run_stage("render.close", close_backend)
        await run_stage("postgres", close_postgres)

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.error("Server shutdown cleanup timed out unfinished=%s", stage)


def create_app(config: AppConfig | None = None, deps: v1.V1Deps | None = None) -> FastAPI:
    """Build a FastAPI app wired with the given or env-driven dependencies.

    Parameters
    ----------
    config:
        Optional ``AppConfig``. If None, ``AppConfig.from_env()`` is used.
    deps:
        Optional ``V1Deps`` for tests. When provided, the backend/store/hub/
        engine_manager/director carried by ``deps`` are wired into the router
        AS-IS and NO LLM/TTS engine loading runs — heavy model loads are the
        caller's responsibility. When None, the components are built from
        ``config`` and the LLM/TTS engines are auto-loaded for BOTH the cloud
        and mock backends (Finding 1: mock = mock RENDER only; LLM/TTS stay
        real per the plan). Only the cloud-specific wiring (the ``cloud``
        side-effect import + ``reconfigure_cloud()``) is skipped for mock.
    """
    if config is None:
        # Build a FRESH AppConfig.from_env() each call instead of reusing the
        # module-level CONFIG. The module-level CONFIG is built once at import
        # time (used by ``main()`` for the port and by the module-level
        # ``app = create_app()`` for uvicorn). Reusing it here made create_app()
        # non-deterministic wrt import order: if core.server was first imported
        # under one env (e.g. cloud), a later create_app() call under a
        # different env (e.g. mock via monkeypatch) still saw the stale cloud
        # CONFIG and tried to build CloudRenderBackend without a key. Reading
        # env fresh each call makes create_app() deterministic and robust to
        # import-order staleness.
        config = AppConfig.from_env()

    # Task 7: reject CORS_ORIGINS='*' for every environment EXCEPT explicit
    # "dev" (covers both the module-level ``app = create_app()`` path and
    # explicit ``create_app``). Finding 3: a deployment with APP_ENV=production
    # or "staging" must NOT bypass the wildcard-CORS rejection that prod gets.
    if config.app_env != "dev" and config.cors_list() == ["*"]:
        raise RuntimeError(
            "CORS_ORIGINS='*' is forbidden outside APP_ENV=dev; set explicit origins"
        )

    # Resolve the optional Postgres runtime store. The injected-deps path
    # carries its own pg_store (or None); the env-driven path builds one from
    # DATABASE_URL when set. Persistence is fire-and-forget; the lifespan
    # connects + applies the schema on startup and closes the pool on shutdown.
    if deps is not None:
        pg = deps.pg_store
    elif config.database_url:
        from .db.postgres_store import PostgresRuntimeStore

        pg = PostgresRuntimeStore(config.database_url)
    else:
        pg = None

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        if pg is not None and getattr(pg, "enabled", False):
            await _connect_postgres(pg)
        yield
        await _shutdown(v1.deps(), pg)

    app = FastAPI(title="VN Live-Commerce Host — core API", lifespan=_lifespan)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: RequestValidationError):
        if any(error["type"] == "string_too_long" for error in exc.errors()):
            return JSONResponse(status_code=413, content={"detail": "input too long"})
        return await request_validation_exception_handler(request, exc)

    app.add_middleware(MaxBodySizeMiddleware, max_bytes=config.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_list(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.api_limiter = SlidingWindowLimiter(
        limit=config.api_rate_limit_requests,
        window_seconds=config.api_rate_limit_window_seconds,
        max_keys=config.api_rate_limit_max_keys,
    )
    app.state.ws_limiter = WebSocketLimiters(
        limit=config.ws_rate_limit_messages,
        window_seconds=config.ws_rate_limit_window_seconds,
        max_keys=config.api_rate_limit_max_keys,
    )

    if deps is not None:
        # Injected path (tests): use deps' components verbatim, skip engine
        # loading, skip Director construction. Wire config through so auth
        # dependencies in v1 can read it.
        backend = deps.backend
        engine_mgr = deps.engine_manager
        director = deps.director
        # If the caller passed a config on deps, prefer it; otherwise use the
        # create_app config so auth gates are always wired.
        deps.config = deps.config or config
        v1.init_deps(deps)
    else:
        # Env-driven path: build everything from config (previous behavior).
        backend = config.build_render_backend()
        store = config.build_store()
        hub = v1.ControlHub()
        engine_mgr = EngineManager()

        # Finding 1: load configured LLM/TTS engines for BOTH cloud AND mock
        # when deps is None. The plan's CORE principle is "mock render = mock
        # rendering ONLY; LLM and TTS remain real." A mock-mode deployment with
        # LLM_ENGINE=llamacpp TTS_ENGINE=transformers must load those real
        # engines so /lite/say uses them instead of silently falling back to
        # echo/tone stubs. Only the cloud-specific wiring (the ``cloud`` import
        # + ``reconfigure_cloud()``) stays cloud-only — the mock backend does
        # not call backend.say(), so reconfigure is not needed there.
        if config.render_backend == "cloud_liveavatar":
            from .render import cloud  # noqa: F401  (side-effect import kept)

        _llm_engine = None
        _tts_engine = None
        try:
            if config.llm.engine not in ("none", "", None):
                _llm_engine = engine_mgr.load_llm(config.llm.to_engine_cfg())
                engine_mgr.set_system_prompt(config.llm.system_prompt)
        except Exception as exc:
            # Record the failure so /health/ready can honestly report
            # not-ready (Finding 2). Dev still falls back to the echo stub so
            # the server starts; prod readiness is reported honestly.
            engine_mgr.llm_load_error = f"{type(exc).__name__}: {exc}"
            print(
                f"[server] LLM engine '{config.llm.engine}' unavailable "
                f"({type(exc).__name__}: {exc}); using echo stub."
            )
            _llm_engine = None

        try:
            if config.tts.engine not in ("tone", "", None):
                _tts_engine = engine_mgr.load_tts(config.tts.to_engine_cfg())
        except Exception as exc:
            engine_mgr.tts_load_error = f"{type(exc).__name__}: {exc}"
            print(
                f"[server] TTS engine '{config.tts.engine}' unavailable "
                f"({type(exc).__name__}: {exc}); using tone stub."
            )
            _tts_engine = None

        if config.render_backend == "cloud_liveavatar":
            engine_mgr.reconfigure_cloud()
            if engine_mgr.llm:
                print(f"[server] LLM  engine={engine_mgr.llm.name}")
            if engine_mgr.tts:
                print(f"[server] TTS  engine={engine_mgr.tts.name} sr={engine_mgr.tts.sample_rate}")
        # Mock backend: no reconfigure_cloud (mock does not call backend.say;
        # /lite/say reads engine_mgr.llm/.tts directly via _streaming_say).

        director = None
        if config.director_enabled:
            from .director.runtime import DirectorRuntime

            director = DirectorRuntime(backend)

        # Wave 2: build DirectorCoordinator when the Director runtime is wired.
        # The coordinator builds a FRESH StreamOrchestrator per _maybe_speak()
        # call (mirroring /lite/say's _streaming_say pattern) so concurrent
        # sessions do not corrupt each other's per-turn state. We pass the
        # factory inputs (llm, tts, backend, chunker_config) instead of a
        # pre-built orchestrator.
        # The shared ``orchestrators`` dict (V1Deps.orchestrators) lets the
        # coordinator register its active per-call queue so the continuous
        # MJPEG endpoint drains utterance frames; ``hub`` lets it emit
        # coordinator.* WS events.
        coordinator = None
        lock_registry = SessionLockRegistry()
        orchestrators: dict = {}
        livekit_publishers = LiveKitPublisherRegistry() if publish_enabled() else None
        if director is not None:
            from .director.coordinator import DirectorCoordinator, CoordinatorConfig
            from .llm.base import _NoopEngine
            from .tts.base import ToneEngine

            _llm_stub = engine_mgr.llm if (engine_mgr.llm is not None) else _NoopEngine()
            _tts_stub = engine_mgr.tts if (engine_mgr.tts is not None) else ToneEngine()
            chunker_config = {
                "text_chunk_min_chars": getattr(config, "text_chunk_min_chars", 12),
                "text_chunk_target_chars": getattr(config, "text_chunk_target_chars", 40),
                "text_chunk_max_chars": getattr(config, "text_chunk_max_chars", 80),
                "text_chunk_flush_timeout_ms": getattr(config, "text_chunk_flush_timeout_ms", 350),
            }
            max_q = getattr(config, "avatar_max_queue_windows", 5)
            coordinator = DirectorCoordinator(
                runtime=director,
                llm=_llm_stub,
                tts=_tts_stub,
                backend=backend,
                chunker_config=chunker_config,
                lock_registry=lock_registry,
                cfg=CoordinatorConfig(tick_ms=300, window_sec=75.0),
                hub=hub,
                orchestrator_registry=orchestrators,
                max_queue_windows=max_q,
                pg_store=pg,
                audio_window_callback=livekit_publishers.publish
                if livekit_publishers is not None
                else None,
            )

        v1.init_deps(
            v1.V1Deps(
                backend=backend,
                store=store,
                hub=hub,
                director=director,
                engine_manager=engine_mgr,
                config=config,
                locks=lock_registry,
                orchestrators=orchestrators,
                coordinator=coordinator,
                pg_store=pg,
                livekit_publishers=livekit_publishers,
            )
        )

    app.include_router(v1.router)
    app.include_router(health.router)

    @app.get("/")
    async def root() -> dict:
        llm_name = (
            engine_mgr.llm.name if engine_mgr is not None and engine_mgr.llm else "none(stub)"
        )
        tts_name = (
            engine_mgr.tts.name if engine_mgr is not None and engine_mgr.tts else "tone(stub)"
        )
        return {
            "service": "vn-live-commerce-host",
            "api": "/api/v1",
            "render_backend": backend.name if backend is not None else None,
            "llm_engine": llm_name,
            "tts_engine": tts_name,
            "director_enabled": director is not None,
            "engine_manager": engine_mgr is not None,
        }

    return app


# Module-level app — built once at import time from env, for uvicorn.
# Under RENDER_BACKEND=mock + LLM_ENGINE=none/TTS_ENGINE=tone (the offline
# default) this does NOT load any heavy model — the engine guards skip the
# load when the configured engine is the stub. Under RENDER_BACKEND=mock with
# a real LLM_ENGINE/TTS_ENGINE set, those real engines ARE loaded (mock = mock
# RENDER only; LLM/TTS stay real per the plan). Under RENDER_BACKEND=cloud
# this runs the engine-loading block + reconfigure_cloud (same as before).
app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
