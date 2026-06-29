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
non-GPU box), it falls back to the built-in stubs so the server still starts.
For explicit control (custom cfg), call core.render.cloud.configure() before
serving — the colab launcher does this.

App factory:
    create_app(config=None, deps=None) -> FastAPI
      - If ``config`` is None, builds it from env (``AppConfig.from_env()``).
      - If ``deps`` is provided (tests), uses the injected ``V1Deps`` as-is
        and does NOT build/load LLM/TTS engines — the caller owns the deps.
      - Otherwise builds backend/store/hub/engine_manager from ``config``,
        runs the cloud engine-loading block when ``render_backend == "cloud"``
        (identical to the previous module-level wiring), and constructs
        ``V1Deps`` from those built components.

A module-level ``app = create_app()`` is kept at the END of the file for
uvicorn compatibility (``uvicorn core.server:app``). Tests can call
``create_app(config=..., deps=...)`` to get an app without loading models.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import v1
from .config import AppConfig
from .engine_manager import EngineManager

# Module-level config — used by ``main()`` for the port and as the default
# for ``create_app()``. Built once at import time from env.
CONFIG = AppConfig.from_env()


def create_app(config: AppConfig | None = None,
               deps: v1.V1Deps | None = None) -> FastAPI:
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
        ``config`` and (for the cloud backend) the LLM/TTS engines are
        auto-loaded, matching the previous module-level wiring exactly.
    """
    if config is None:
        config = CONFIG

    app = FastAPI(title="VN Live-Commerce Host — core API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_list(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if deps is not None:
        # Injected path (tests): use deps' components verbatim, skip engine
        # loading, skip Director construction.
        backend = deps.backend
        engine_mgr = deps.engine_manager
        director = deps.director
        v1.init_deps(deps)
    else:
        # Env-driven path: build everything from config (previous behavior).
        backend = config.build_render_backend()
        store = config.build_store()
        hub = v1.ControlHub()
        engine_mgr = EngineManager()

        if config.render_backend == "cloud":
            from .render import cloud  # noqa: F401  (side-effect import kept)

            _llm_engine = None
            _tts_engine = None
            try:
                if config.llm.engine not in ("none", "", None):
                    _llm_engine = engine_mgr.load_llm(config.llm.to_engine_cfg())
                    engine_mgr.set_system_prompt(config.llm.system_prompt)
            except Exception as exc:
                print(f"[server] LLM engine '{config.llm.engine}' unavailable "
                      f"({type(exc).__name__}: {exc}); using echo stub.")
                _llm_engine = None

            try:
                if config.tts.engine not in ("tone", "", None):
                    _tts_engine = engine_mgr.load_tts(config.tts.to_engine_cfg())
            except Exception as exc:
                print(f"[server] TTS engine '{config.tts.engine}' unavailable "
                      f"({type(exc).__name__}: {exc}); using tone stub.")
                _tts_engine = None

            engine_mgr.reconfigure_cloud()
            if engine_mgr.llm:
                print(f"[server] LLM  engine={engine_mgr.llm.name}")
            if engine_mgr.tts:
                print(f"[server] TTS  engine={engine_mgr.tts.name} "
                      f"sr={engine_mgr.tts.sample_rate}")
        elif config.render_backend == "mock":
            # Mock renderer needs no LLM/TTS cloud wiring and no
            # LIVEAVATAR_API_KEY. LLM/TTS engines are not built here; the mock
            # consumes AudioWindows produced upstream and does not call
            # backend.say().
            pass

        director = None
        if config.director_enabled:
            from .director.runtime import DirectorRuntime

            director = DirectorRuntime(backend)

        v1.init_deps(v1.V1Deps(
            backend=backend,
            store=store,
            hub=hub,
            director=director,
            engine_manager=engine_mgr,
        ))

    app.include_router(v1.router)

    @app.get("/")
    async def root() -> dict:
        llm_name = engine_mgr.llm.name if engine_mgr is not None and engine_mgr.llm else "none(stub)"
        tts_name = engine_mgr.tts.name if engine_mgr is not None and engine_mgr.tts else "tone(stub)"
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
# Under RENDER_BACKEND=mock + empty LIVEAVATAR_API_KEY this does NOT load any
# heavy model (the mock branch skips engine loading). Under RENDER_BACKEND=cloud
# this runs the engine-loading block (same as before the refactor).
app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
