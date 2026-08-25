# ai-livestream-commerce-vn

Real-time Vietnamese **AI live-commerce host** — a talking avatar that perceives viewer
chat, reasons about products, and replies with natural Vietnamese speech + lip-synced video
at low latency.

This repo is the **`implementations/`** workspace: a production-standard, portable backend
with a versioned `/api/v1`, a swappable avatar renderer (LiveAvatar cloud now, self-host
diffusion later), and a backend-agnostic Director that clusters viewer comments and decides
what the host says.

## Architecture (two planes)

```
            CONTROL plane (this backend)             MEDIA plane (NOT this backend)
 Browser ────HTTP /api/v1 + WS──► backend_service ──► Renderer (LiveAvatar cloud / self-host)
(browser) ◄──WS events─────────  (control)            │
    ▲                                                 │
    └──────────── WebRTC video via LiveKit ◄──────────┘
```

- **Control plane** = `services/product/backend_service/src/backend/` FastAPI package
  (`/api/v1`, JSON + WebSocket): session lifecycle, `say`, interrupt, Director ingest, events.
- **Media plane** = avatar video, streamed renderer → LiveKit → browser directly. Frames
  never transit this backend. Browser only gets `livekit_url` + `livekit_client_token`;
  secrets stay server-side.

## Layout

```
services/product/backend_service/  canonical FastAPI control-plane package
services/product/llm_service/      self-host LLM package
services/product/tts_service/      self-host TTS package
services/product/avatar_service/   self-host avatar package
services/platform/                 LiveKit, LMCache, Postgres, and Redis runtime assets
backend clients/ liveavatar_sdk  LiveAvatar cloud SDK (backend-owned client)
workbench/                         developer console (Vite/TS) for canonical /api/v1
notebooks/                          bootstrap_colab.ipynb (clone → weights → run → ngrok)
docs/                               confirmed design + Seoul pricing (see docs/README.md)
plans/                              active implement plans (00 AWS stack, 01 app backlog)
archived/                           historical material
```

## Quick start (offline)

```powershell
uv sync --extra test
$env:RENDER_BACKEND = "mock"
$env:LLM_ENGINE = "none"
$env:TTS_ENGINE = "tone"
$env:DIRECTOR_ENABLED = "0"
$env:APP_ENV = "dev"
uv run pytest tests/ci/ -q
uv run --project services/product/backend_service uvicorn backend.main:app --port 8800
```

Open the `workbench/` developer console and paste the backend origin; it
appends `/api/v1` itself. The mock path needs no LiveAvatar key.

## Runtime configuration

```text
APP_ENV=dev|prod
RENDER_BACKEND=mock|cloud_liveavatar|self_host_*
SESSION_STORE=memory|redis
LLM_ENGINE=none|vllm|sglang|hf|llamacpp
TTS_ENGINE=tone|transformers|vieneu|cosyvoice
# Remote provider endpoints: LLM_BASE_URL / TTS_BASE_URL / AVATAR_BASE_URL
DATABASE_URL=postgresql://...              # optional runtime persistence
LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
LIVEKIT_PUBLISH=0|1                        # requires valid LiveKit credentials
```

LiveKit room-token minting, frontend subscription, and backend PCM publishing
are implemented and offline-covered. A real SFU/browser media smoke, avatar
video publishing, Pipecat cutover, Redis Streams, and GPU benchmarks remain
unverified. Keep `LIVEKIT_PUBLISH=0` for API-only Tier S.

## References

- Current backend state: [docs/architecture.md](docs/architecture.md)
- Colab vLLM demo: [docs/runbook-colab.md](docs/runbook-colab.md)
- Deployment preparation: [docs/runbook-deploy-prep.md](docs/runbook-deploy-prep.md)
- Tier S apply/smoke/teardown: [docs/runbook-live-smoke-and-teardown.md](docs/runbook-live-smoke-and-teardown.md)
