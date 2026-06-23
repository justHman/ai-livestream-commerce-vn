"""core.server — production FastAPI app for the VN live-commerce host.

Mounts the versioned `/api/v1` router and wires the selected RenderBackend +
SessionStore from env (see core/config.py). This is the entrypoint you run on
Colab (single process, InMemory store, cloud renderer) and lift to AWS
(Redis store, sticky LB) by changing ONLY environment variables.

Run:
    uv run uvicorn core.server:app --port 8800
    # or: python -m core.server

Env:
    RENDER_BACKEND=cloud|self_host   (default cloud)
    SESSION_STORE=memory|redis       (default memory)
    REDIS_URL=redis://...            (when SESSION_STORE=redis)
    CORS_ORIGINS=*|https://a,https://b
    LIVEAVATAR_API_KEY=...           (cloud backend; backend-only secret)
    PORT=8800

To inject real LLM/TTS for the cloud backend, call
`core.render.cloud.configure(llm_fn=..., tts_fn=...)` before serving
(the Colab launcher does this).
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

_director = None
if CONFIG.director_enabled:
    from .director.runtime import DirectorRuntime

    _director = DirectorRuntime(_backend)

v1.init_deps(v1.V1Deps(backend=_backend, store=_store, hub=_hub, director=_director))

app.include_router(v1.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "vn-live-commerce-host",
        "api": "/api/v1",
        "render_backend": _backend.name,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
