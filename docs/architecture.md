# Architecture — VN Live-Commerce Host

Living architecture reference for the production backend. Design intent, module
layout, key seams, and deployment model. Source of truth; outdated docs in this
directory point here.

## 1. Two planes (control vs media)

```
                CONTROL plane (this backend)               MEDIA plane (NOT this backend)
  Frontend ──HTTP /api/v1 + WS──>  core server  ──>  Renderer (LiveAvatar cloud / self-host)
 (browser) <──WS events──────────  (control)               │
     ^                                                      │
     └────────────── WebRTC video via LiveKit ◄────────────┘
```

- **Control plane** = `core/` FastAPI app: session lifecycle, say, interrupt, events, Director decisions. JSON + WebSocket.
- **Media plane** = avatar video. Flows renderer -> LiveKit -> browser directly. Frames never transit our backend.
- Browser receives only frontend-safe creds: `livekit_url` + `livekit_client_token`. Secrets stay backend-side.
- The frontend (`frontend/lite.html`) is a thin client of the API + a LiveKit token.

## 2. Module layout

```
implementations/
+-- core/                           # Production surface (transport-agnostic)
|   +-- __init__.py
|   +-- config.py                   # AppConfig, LLMConfig, TTSConfig (env-driven)
|   +-- server.py                   # create_app() factory + module-level app for uvicorn
|   +-- store.py                    # SessionStore: InMemorySessionStore | RedisSessionStore
|   +-- engine_manager.py           # EngineManager (runtime swap + preset registry)
|   +-- api/
|   |   +-- __init__.py
|   |   +-- auth.py                 # viewer_auth, admin_auth, validate_ws_token (Task 7)
|   |   +-- v1.py                   # /api/v1 router, ControlHub, V1Deps, all endpoints
|   +-- render/
|   |   +-- __init__.py             # Exports RenderBackend, FullPipelineBackend, StreamingAvatarBackend
|   |   +-- base.py                 # ABCs: RenderBackend, FullPipelineBackend, StreamingAvatarBackend
|   |   +-- cloud.py                # CloudRenderBackend (FullPipelineBackend) -- LiveAvatar cloud
|   |   +-- self_host.py            # SelfHostRenderBackend stub (future diffusion model)
|   |   +-- mock.py                 # MockRenderBackend (StreamingAvatarBackend) -- PIL frames, offline
|   |   +-- windows.py              # TextChunk, AudioWindow, VideoWindow dataclasses + helpers
|   |   +-- locks.py                # SessionLockRegistry (per-session non-blocking lock)
|   |   +-- queue.py                # BoundedVideoQueue + CoordinatorMetrics
|   |   +-- orchestrator.py         # StreamOrchestrator (LLM->chunker->TTS->backend streaming pipeline)
|   +-- tts/
|   |   +-- __init__.py             # TTSEngine ABC + load_engine + to_tts_fn
|   |   +-- base.py                 # TTSEngine, ToneEngine, TTSRequest, AudioChunk
|   |   +-- adapters/
|   |       +-- vieneu.py           # VieNeu-TTS adapter (Apache, VN-native)
|   |       +-- cosyvoice.py        # CosyVoice2 adapter (Apache, streaming)
|   |       +-- transformers.py     # HF transformers TTS adapter (universal fallback)
|   +-- llm/
|   |   +-- __init__.py             # LLMEngine ABC + load_engine + to_llm_fn
|   |   +-- base.py                 # LLMEngine, LLMRequest, LLMResponse, _NoopEngine
|   |   +-- adapters/
|   |       +-- llamacpp.py         # llama.cpp GGUF adapter (Colab T4, low VRAM)
|   |       +-- vllm.py             # vLLM adapter (production, continuous batching)
|   |       +-- sglang.py           # SGLang adapter (RadixAttention prefix cache)
|   |       +-- transformers.py     # HF transformers adapter (universal fallback)
|   +-- stream/
|   |   +-- __init__.py
|   |   +-- chunker.py              # TextChunker (token deltas -> phrase-sized TextChunks)
|   +-- director/
|   |   +-- __init__.py             # Director FSM, Decision, StreamState, etc.
|   |   +-- director.py             # Director core FSM
|   |   +-- runtime.py              # DirectorRuntime (wraps Director + per-session state)
|   |   +-- coordinator.py          # DirectorCoordinator (background tick loop)
|   |   +-- state.py                # Phase, ProductState, StreamState, TrafficMode
|   |   +-- config.py               # StreamConfig (dashboard-writable policy)
|   |   +-- embedder.py             # VN bi-encoder embeddings
|   |   +-- cluster.py              # Comment clustering
|   |   +-- scorer.py               # rank_clusters, retrieve_product
|   |   +-- catalog.py              # Product, ProductVariant, route_intent_to_field
|   |   +-- hooks.py                # HookPool (pre-generated engagement lines)
|   |   +-- chat_queue.py           # ChatQueue (per-session rolling window)
|   +-- debug/
|   |   +-- __init__.py
|   |   +-- mock_data.py            # MOCK_PRODUCTS, MOCK_VIEWER_MSGS
|   |   +-- traffic_sim.py          # TrafficSimulator
|   |   +-- smoke.py                # Smoke test runner
|   +-- tests/
|       +-- v1_smoke_test.py
|       +-- director_smoke_test.py
|       +-- director_api_smoke_test.py
|       +-- test_app_factory.py
|       +-- test_api_auth.py / test_ws_auth.py
|       +-- test_mock_render_lifecycle.py / test_mock_frame_generation.py
|       +-- test_llm_streaming.py / test_tts_streaming.py
|       +-- test_text_chunker.py / test_audio_windowing.py
|       +-- test_session_concurrency.py / test_queue_coordinator.py
|       +-- test_chat_queue.py / test_director_coordinator.py
|       +-- test_engines_endpoint.py / test_mjpeg_continuous.py
|       +-- test_lite_chat_integration.py / test_idle_loop.py
+-- providers/
|   +-- __init__.py
|   +-- liveavatar_cloud/           # LiveAvatar cloud SDK (behind CloudRenderBackend adapter)
|       +-- backend/                # client.py, conversation.py, lite_agent.py, audio.py, etc.
|       +-- examples/               # smoke tests, colab_deploy.py
+-- frontend/
|   +-- lite.html                   # Primary frontend demo
|   +-- index.html                  # Static landing page
+-- notebooks/
|   +-- bootstrap_colab.ipynb       # Step-by-step Colab bootstrap
|   +-- colab_demo.ipynb            # Interactive demo notebook
+-- archive/
|   +-- legacy-liveavatar-demo/     # Pre-refactor mock diffusion PoC (archived)
+-- docs/
|   +-- architecture.md             # THIS FILE
|   +-- PRODUCTION.md               # Historical -- see architecture.md
|   +-- PLAN.md                     # Historical -- see architecture.md
|   +-- TASKS.md                    # Historical -- see architecture.md
|   +-- BACKEND_PRODUCTION_FIX_PLAN.md  # Historical fix plan (items now DONE)
|   +-- runbook-colab.md            # Colab deployment runbook
|   +-- checklists/
|       +-- colab-readiness.md
|       +-- release.md
```

## 3. RenderBackend seam

All renderers share a session-lifecycle base, then split by protocol:

```
RenderBackend (ABC)
  start(opts) -> StartResult      (blocking, off-loop via asyncio.to_thread)
  interrupt(session_id) -> None
  stop(session_id) -> None
  session_status(session_id) -> str   (default "unknown")
  |
  +-- FullPipelineBackend(RenderBackend)
  |     say(session_id, text, generate=True) -> str
  |     CloudRenderBackend -- LiveAvatar cloud; owns LLM+TTS+avatar internally
  |
  +-- StreamingAvatarBackend(RenderBackend)
        stream_audio(session_id, audio_window) -> Iterator[VideoWindow]
        MockRenderBackend -- PIL-synthesized frames, fully offline, no LiveKit
        SelfHostRenderBackend -- future diffusion model (stub, NotImplemented)
```

Select with `RENDER_BACKEND=cloud|mock|self_host`. The API layer (`/lite/say`)
branches on `isinstance(backend, StreamingAvatarBackend)` to choose the streaming
pipeline vs the cloud's `backend.say()` path.

## 4. Streaming pipeline (mock + future self-host)

When `RENDER_BACKEND=mock`, `/lite/say` routes through `_streaming_say`:

```
LLM stream_chunks()
  -> TextChunker (token deltas -> phrase-sized TextChunks)
    -> TTS stream_audio() (TextChunk -> Iterator[AudioWindow])
      -> backend.stream_audio() (AudioWindow -> Iterator[VideoWindow])
        -> BoundedVideoQueue (drop-oldest on overflow)
```

All three stages are sync generators running in one `asyncio.to_thread` worker.
A thread-safe `queue.Queue` bridge carries `VideoWindow` objects to the async
side. `cancel()` sets a `threading.Event` checked between each stage step.

For cloud (`FullPipelineBackend`), the orchestrator is NOT used -- the existing
`backend.say()` path handles the whole turn internally.

## 5. Director FSM

Between viewer comments and the renderer sits the Director:

```
Viewer comments -> Comment clustering (VN bi-encoder, cosine)
  -> Phase-aware scoring (opening / selling / closing)
    -> Director FSM decides action (idle / skip / hook / answer / product / close)
      -> StreamOrchestrator (for StreamingAvatarBackend) or backend.say()
```

Two-tier retrieval:
- **TIER1**: semantic product match via vector cosine (bkai vietnamese-bi-encoder)
- **TIER2**: O(1) structured-field lookup that grounds an LLM prompt

Two operating modes:
- **Sync path** (`/lite/ingest`): POST batch comments -> Director decides -> speaks
- **Async coordinator** (`/lite/chat`): POST one comment -> ChatQueue -> background
  tick loop (300ms) drains queue -> Director decides -> orchestrator runs

The coordinator supports barge-in: if a higher-score cluster arrives while
speaking and `may_interrupt` is set, the current utterance is cancelled.

## 6. LLM engine seam

```
LLMEngine (ABC)
  generate(req) -> LLMResponse     (full response)
  stream_chunks(req) -> Iterator[TextChunk]   (token deltas, new in Wave 2)
  unload() -> None                 (free VRAM)
  warmup(system_prompt) -> None

Engines: llamacpp | vllm | sglang | hf | none (echo stub)
```

`to_llm_fn(engine)` wraps the engine as a `(text) -> str` callable for the cloud
RenderBackend.

Key env vars: `LLM_ENGINE`, `LLM_MODEL`, `LLM_MODEL_PATH`, `LLM_STREAM`.

## 7. TTS engine seam

```
TTSEngine (ABC)
  synthesize(req) -> AudioChunk                  (full waveform, blocking)
  stream(req) -> Iterator[AudioChunk]             (optional, if model streams)
  unload() -> None
  warmup() -> None

Adapters: vieneu | cosyvoice | transformers | tone (no-deps stub)
```

`to_tts_fn(engine)` wraps as a `(text) -> (bytes, rate)` callable.

6 presets registered in `engine_manager.py` for frontend dropdown (Phase A):
vieneu-v3-turbo, vieneu-v2, cosyvoice2, kokoro, xtts-v2, transformers-mms-vi.

## 8. EngineManager -- runtime swap

`EngineManager` holds the loaded LLM/TTS singleton. When the user swaps from the
UI (`POST /engines/llm` or `/engines/tts`):

1. Load the new engine (slow: 10-30s for a 4B model).
2. Unload the old engine (free VRAM).
3. Reconfigure the cloud RenderBackend with the new engines.

Swap is serialized via `threading.Lock`. During a swap, generation calls block
briefly -- acceptable for demo.

## 9. API surface

| Endpoint | Auth | Description |
|------|------|-------------|
| `GET /health` | none | Service info |
| `GET /health/live` | none | Liveness probe |
| `GET /health/ready` | none | Readiness probe (backend + engines) |
| `POST /lite/start` | viewer | Create session, return LiveKit token |
| `POST /lite/say` | viewer | One turn: LLM -> TTS -> avatar |
| `POST /lite/interrupt` | viewer | Barge-in |
| `POST /lite/stop` | viewer | Stop + cleanup |
| `POST /lite/attach` | viewer | Attach Director + product catalog |
| `POST /lite/ingest` | viewer | Batch comments -> Director decides -> speaks |
| `POST /lite/chat` | viewer | Single comment -> coordinator queue (202 Accepted) |
| `WS /ws/control/{id}` | viewer | Per-session event stream |
| `GET /engines` | admin | List presets + loaded engines |
| `POST /engines/llm` | admin | Swap LLM engine |
| `POST /engines/tts` | admin | Swap TTS engine |
| `POST /engines/tts/preset` | admin | Select TTS preset |
| `GET /mock/frame/{id}.png` | none | Mock: latest frame as PNG |
| `GET /mock/video/{id}.mjpeg` | none | Mock: continuous MJPEG stream |
| `GET /mock/status/{id}` | none | Mock: session status |
| `POST /debug/start` | admin+debug | Start traffic simulator |
| `POST /debug/stop` | admin+debug | Stop traffic simulator |
| `GET /debug/status/{id}` | admin+debug | Debug status |
| `GET /debug/mock_products` | admin+debug | Mock product catalog |
| `GET /debug/mock_viewer_msgs` | admin+debug | Mock viewer message pool |

## 10. Auth model

Two auth planes:
- **VIEWER** (`BACKEND_API_TOKEN`): `/lite/*` + `/ws/control/{id}`
- **ADMIN** (`ADMIN_API_TOKEN`): `/engines/*` + `/debug/*`

Rules:
- `APP_ENV=dev` + token empty -> auth disabled (local dev + existing tests)
- `APP_ENV=prod` + missing token -> 401
- Valid viewer token on admin endpoint -> 403
- WS token validated via query parameter BEFORE `ws.accept()`

## 11. Portability -- same code, Colab -> AWS (env only)

| Dimension | Colab (now) | AWS (later) |
|-----------|-------------|-------------|
| RENDER_BACKEND | cloud (or mock) | cloud (or self_host) |
| SESSION_STORE | memory (InMemorySessionStore) | redis (RedisSessionStore) |
| Process model | 1 uvicorn process | N containers behind ALB (sticky sessions) |
| LLM | llamacpp GGUF (local) | vLLM (shared endpoint, prefix cache) |
| TTS | transformers/vieneu (local) | shared TTS endpoint |
| Public access | ngrok tunnel | ALB + CloudFront |
| Config | `AppConfig.from_env()` in Colab cell | Task definition env vars + Secrets Manager |

## 12. Session store

`SessionStore` ABC with two implementations:
- **InMemorySessionStore**: dict-backed, single-process, no persistence.
- **RedisSessionStore**: `redis.asyncio` client, TTL-based expiry, keyed as `session:{id}`.

Stores only JSON session metadata (status, mode) -- NOT live WS/agent objects,
which stay in-process on the owning instance. Cross-instance coordination
(`owner_instance_id`, 409 wrong_instance, 410 session_lost) is deferred.

## 13. Cost of LiveAvatar cloud

| Mode | Cost | Notes |
|------|------|-------|
| Sandbox | 0 credits | All development |
| FULL | 2 credits/min | Cloud runs LLM+TTS |
| LITE | 1 credit/min | We run LLM+TTS, cloud only renders |
| Free tier | 10 credits | ~10 min LITE |
| Paid | ~$0.10/credit | LITE ~$0.10/min |

## 14. KV cache + inference strategy

For Colab T4 (low concurrency): llamacpp GGUF Q4_K_M (~3GB VRAM, low TTFT).
For production (many sessions): vLLM with prefix caching + FP8 KV cache + chunked prefill.
Prefix caching caches the persona + catalog system prompt KV once, reused across
all user turns -- the main technical reason for vLLM/SGLang over llamacpp.

FlashAttention-2/3 and SageAttention2 are not available on T4 (sm_75).

## 15. Models

| Role | Model | License | Notes |
|------|-------|---------|-------|
| LLM | gemma-3-4b-it (GGUF Q4_K_M) | Gemma terms, gated | Default on Colab |
| LLM | Qwen3-4B (GGUF Q4_K_M) | Apache-2.0 | Alternative |
| LLM | Qwen3.5-4B (GGUF Q4_K_M) | Apache-2.0 | Latest, 4K context default |
| LLM | SeaLLMs-v3-7B-Chat | SeaLLMs terms | Stronger VN, ~1.6x slower |
| TTS | VieNeu-TTS-v3-Turbo | Apache-2.0 | VN-native, 48kHz |
| TTS | CosyVoice2-0.5B | Apache-2.0 | Streaming |
| TTS | facebook/mms-tts-vie | Apache-2.0 | HF transformers fallback |
| Embed | bkai vietnamese-bi-encoder | Apache | Clustering + retrieval |

## 16. Run

```bash
# Offline demo (mock render, no API key, no model)
RENDER_BACKEND=mock uv run uvicorn core.server:app --port 8800

# Cloud mode (needs LIVEAVATAR_API_KEY)
export LIVEAVATAR_API_KEY=...
RENDER_BACKEND=cloud uv run uvicorn core.server:app --port 8800

# Full production (with LLM + TTS)
LLM_ENGINE=llamacpp LLM_MODEL_PATH=weights/llm TTS_ENGINE=vieneu \
  TTS_WEIGHTS=weights/tts uv run uvicorn core.server:app --port 8800

# Smoke test
uv run python -m core.tests.v1_smoke_test

# Offline pytest suite
uv run pytest core/tests/ -v

# Frontend: open frontend/lite.html, paste backend URL.
```

## 17. API contract for cloud vs streaming backends

```
cloud (FullPipelineBackend):
  /lite/say -> backend.say(session_id, text) -> str    (blocking, off-loop)

mock (StreamingAvatarBackend):
  /lite/say -> StreamOrchestrator.run() -> str          (async streaming pipeline)
    LLM.stream_chunks -> TextChunker -> TTS.stream_audio -> backend.stream_audio
```

The `_streaming_say` function in `v1.py` branches on `isinstance`. For the cloud
backend, `say()` runs as a single `asyncio.to_thread` call. For the streaming
backend, the orchestrator runs the four-stage pipeline in a worker thread,
pushing `VideoWindow` objects into `BoundedVideoQueue` that the MJPEG endpoint
drains for continuous playback.
