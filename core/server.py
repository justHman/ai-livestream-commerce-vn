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
    RENDER_BACKEND=cloud|self_host
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
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import v1
from .config import AppConfig

CONFIG = AppConfig.from_env()

app = FastAPI(title="VN Live-Commerce Host — core API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONFIG.cors_list(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire dependencies: selected renderer + store + control hub + Director runtime.
_backend = CONFIG.build_render_backend()
_store = CONFIG.build_store()
_hub = v1.ControlHub()

# Auto-build LLM + TTS engines from env and inject into the cloud backend.
# Falls back to stubs if the engine's deps are missing (keeps the server runnable
# on non-GPU / CI). The colab launcher can override via configure() afterwards.
from .engine_manager import EngineManager

_engine_mgr = EngineManager()

_llm_engine = None
_tts_engine = None
if CONFIG.render_backend == "cloud":
    from .render import cloud

    try:
        if CONFIG.llm.engine not in ("none", "", None):
            _llm_engine = _engine_mgr.load_llm(CONFIG.llm.to_engine_cfg())
            _engine_mgr.set_system_prompt(CONFIG.llm.system_prompt)
    except Exception as exc:
        print(f"[server] LLM engine '{CONFIG.llm.engine}' unavailable "
              f"({type(exc).__name__}: {exc}); using echo stub.")
        _llm_engine = None

    try:
        if CONFIG.tts.engine not in ("tone", "", None):
            _tts_engine = _engine_mgr.load_tts(CONFIG.tts.to_engine_cfg())
    except Exception as exc:
        print(f"[server] TTS engine '{CONFIG.tts.engine}' unavailable "
              f"({type(exc).__name__}: {exc}); using tone stub.")
        _tts_engine = None

    _engine_mgr.reconfigure_cloud()
    if _engine_mgr.llm:
        print(f"[server] LLM  engine={_engine_mgr.llm.name}")
    if _engine_mgr.tts:
        print(f"[server] TTS  engine={_engine_mgr.tts.name} sr={_engine_mgr.tts.sample_rate}")
elif CONFIG.render_backend == "mock":
    # Mock renderer needs no LLM/TTS cloud wiring and no LIVEAVATAR_API_KEY.
    # LLM/TTS engines are not built here; the mock consumes AudioWindows
    # produced upstream and does not call backend.say().
    pass

# Director runtime (optional orchestration layer).
_director = None
if CONFIG.director_enabled:
    from .director.runtime import DirectorRuntime

    _director = DirectorRuntime(_backend)

v1.init_deps(v1.V1Deps(backend=_backend, store=_store, hub=_hub, director=_director,
                        engine_manager=_engine_mgr))

app.include_router(v1.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "vn-live-commerce-host",
        "api": "/api/v1",
        "render_backend": _backend.name,
        "llm_engine": _engine_mgr.llm.name if _engine_mgr.llm else "none(stub)",
        "tts_engine": _engine_mgr.tts.name if _engine_mgr.tts else "tone(stub)",
        "director_enabled": _director is not None,
        "engine_manager": True,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
