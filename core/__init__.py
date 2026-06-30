"""core — transport-agnostic product surface for the VN live-commerce host.

This package is the real production entrypoint. It does NOT know whether the
avatar is rendered by LiveAvatar cloud or a future self-host diffusion model —
that is hidden behind the RenderBackend interface (core/render/base.py).

Layout:
  core/server.py        FastAPI app, mounts the versioned API
  core/api/v1.py        /api/v1/* routes (stable public contract)
  core/render/base.py   RenderBackend lifecycle + FullPipeline/Streaming protocols
  core/render/cloud.py  adapter over providers.liveavatar_cloud (LiveAvatar cloud)
  core/render/self_host.py  future self-host streaming renderer (stub)
  core/config.py        env-driven AppConfig (+ RENDER_BACKEND selector)
  core/store.py         SessionStore: InMemory (Colab) | Redis (AWS)
  core/director/        viewer-comment clustering + phase scoring + coordinator
"""
