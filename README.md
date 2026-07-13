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

## Quick start (local / sandbox — free, no credits)

```bash
# 1. secret (backend-only; never sent to the browser)
export LIVEAVATAR_API_KEY=...          # or put it in .env (gitignored)
export RENDER_BACKEND=cloud SESSION_STORE=memory

# 2. verify the API end-to-end against the LiveAvatar sandbox
python -m core.tests.v1_smoke_test

# 3. run the backend
uv run uvicorn core.server:app --port 8800
#   or: python -m core.server

# 4. open the demo frontend, paste the backend URL, click Start
python -m http.server 8901 --directory frontend
```

Director loop (cluster comments → decide → avatar speaks):
```bash
DIRECTOR_ENABLED=1 python -m core.tests.director_api_smoke_test
```

## Colab (GPU) — see `notebooks/bootstrap_colab.ipynb`
Clone this repo → install deps → download LLM (gemma-3-4b / Qwen3-4B Q4_K_M GGUF) + TTS
(VieNeu-TTS) weights → run `core.server` → expose via ngrok → connect the frontend.

## Config (env)

```
RENDER_BACKEND   cloud | self_host(future)
SESSION_STORE    memory(Colab) | redis(AWS)
DIRECTOR_ENABLED 0 | 1
TTS_ENGINE       vieneu(default) | kokoro | cosyvoice | xtts | tone
LIVEAVATAR_API_KEY   backend-only secret (gitignored .env)
```

See `PRODUCTION.md` for the full architecture, portability (Colab → AWS), and model choices.
