# ai-livestream-commerce-vn — `implementations/`

VN AI live-commerce host backend: FastAPI control plane + LiveKit media plane. Two planes split — control (this repo, `services/product/backend_service/`) vs media (renderer → LiveKit → browser directly; frames never transit this backend).

## Commands

```bash
uv sync                                    # install backend deps from uv.lock
uv run pytest tests/ci/ -q               # repository-tool tests
uv run --project services/product/backend_service pytest tests/unit/ -q   # backend unit tests
uv run --project services/product/backend_service uvicorn backend.main:app --port 8800 # canonical backend entrypoint
uvx ruff check . && uvx ruff format .      # lint + format
```

CI offline env: `RENDER_BACKEND=mock LLM_ENGINE=none TTS_ENGINE=tone DIRECTOR_ENABLED=0 APP_ENV=dev`.

## Architecture

- **Control plane** = `services/product/backend_service/` FastAPI: `/api/v1/*` (REST + WS), session lifecycle, Director (cluster viewer comments → decide → speak). JSON + WS only.
- **Media plane** = avatar video flows renderer → LiveKit → browser directly. Browser gets only `livekit_url` + `livekit_client_token`; **secrets stay server-side**, never sent to browser.
- **Engine seams** (env-swappable, same code Colab → AWS): `RENDER_BACKEND` (mock/cloud_liveavatar/self_host_*), `LLM_ENGINE`, `TTS_ENGINE`, `SESSION_STORE` (memory|redis). Director FSM + run-plan cursor + BiEncoder coverage, reactive > proactive per 300ms tick.

## Domain

- **Director** = decides what the host says next from clustered viewer comments + a run plan.
- **RenderBackend seam** = ABC: `FullPipelineBackend` (cloud) vs `StreamingAvatarBackend` (mock/self-host).
- **Runtime DB** (Postgres, ours) vs **business DB** (team SE owns `/user/*` `/shop/*` — we do NOT code those).

## Workflow & Don'ts

- Branches: `main` (prod, tag `v*` → prod), `develop` (integration, auto DEV deploy), `feature/*` (PR → develop). Conventional commits `feat/fix/docs(scope):`. No `Co-Authored-By` trailers. Never commit `.env`. Nested git repo — keep work path-scoped here.
- Don't edit generated/runtime dirs: `.runtime/`, `.codegraph/`, `.venv/`, `__pycache__/`, `archived/`, `uv.lock` (use `uv lock`).
- Don't reintroduce for MVP: NAT, private subnet, ECR, Secrets Manager, Route53, AWS WAF, MJPEG as prod media, llama.cpp as prod LLM, head-only avatar. (See `docs/aws-architecture.md` §9.)
- Don't implement `/user/*` or `/shop/*` — team SE owns those + business DB.