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
 Frontend ──HTTP /api/v1 + WS──► core server ──► Renderer (LiveAvatar cloud / self-host)
(browser) ◄──WS events─────────  (control)            │
    ▲                                                 │
    └──────────── WebRTC video via LiveKit ◄──────────┘
```

- **Control plane** = `core/` FastAPI app (`/api/v1`, JSON + WebSocket): session lifecycle,
  `say`, interrupt, Director ingest, events.
- **Media plane** = avatar video, streamed renderer → LiveKit → browser directly. Frames
  never transit this backend. Browser only gets `livekit_url` + `livekit_client_token`;
  secrets stay server-side.

## Layout

```
core/                  production surface (transport-agnostic)
  server.py            FastAPI app; mounts /api/v1; env-wired
  api/v1.py            /api/v1 routes + WS control hub + Director endpoints
  render/              RenderBackend seam: base / cloud (LiveAvatar) / self_host (stub)
  tts/                 TTSEngine seam: base + adapters (vieneu/kokoro/cosyvoice/xtts) + tone
  director/            viewer-comment clustering + phase scoring + FSM (backend-agnostic)
  config.py store.py   env-driven config + SessionStore (InMemory | Redis)
  tests/               sandbox + offline smoke tests
providers/liveavatar_cloud/        LiveAvatar cloud SDK (behind the cloud RenderBackend)
archive/legacy-liveavatar-demo/   earlier mock diffusion PoC (archived)
notebooks/             bootstrap_colab.ipynb (clone → weights → run → ngrok)
docs/                  confirmed design + Seoul pricing (see docs/README.md)
plans/                 active implement plans (00 AWS stack, 01 app backlog)
archive/               legacy demo + docs-historical/
```

## Quick start (offline)

```powershell
uv sync --extra test
$env:RENDER_BACKEND = "mock"
$env:LLM_ENGINE = "none"
$env:TTS_ENGINE = "tone"
$env:DIRECTOR_ENABLED = "0"
$env:APP_ENV = "dev"
uv run pytest core/tests/ -q
uv run uvicorn core.server:app --port 8800
```

Open `frontend/lite.html` through a static server and paste the server origin;
the page appends `/api/v1` itself. The mock path needs no LiveAvatar key.

## Runtime configuration

```text
APP_ENV=dev|prod
RENDER_BACKEND=mock|cloud_liveavatar|remote_avatar|self_host_*
SESSION_STORE=memory|redis
LLM_ENGINE=none|openai_compat|vllm|sglang|hf|llamacpp
TTS_ENGINE=tone|remote_http|transformers|vieneu|cosyvoice
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
