# PLAN.md -- Historical

> This doc is historical. See `architecture.md` for the current design.
>
> Key changes since this was written:
> - `liveavatar_api/` -> `providers/liveavatar_cloud/` (provider path refactored)
> - `liveavatar_api_cloud/` -> `providers/liveavatar_cloud/` (typo fix)
> - `providers/liveavatar_cloud_cloud/` -> `providers/liveavatar_cloud/` (double suffix removed)
> - Director module (`core/director/`) implemented: FSM + clustering + scoring + coordinator
> - TTS seam (`core/tts/`) implemented: vieneu, cosyvoice, transformers adapters + ToneEngine
> - LLM streaming (`core/llm/stream_chunks`) implemented
> - Mock renderer + streaming orchestrator implemented
> - Auth gates, app factory, session locks all implemented
> - DirectorCoordinator background tick loop implemented
> - Qwen3.5-4B GGUF preset registered

# PLAN — VN Live-Commerce Host (implementations/) (original, preserved below)

## Goal
A production-standard, portable live-commerce avatar stack: versioned `/api/v1`, a swappable
renderer (LiveAvatar cloud now, self-host diffusion later), Colab→AWS portability via env, a
Colab bootstrap notebook, and a backend-agnostic Director (next phase).

## Architecture (see PRODUCTION.md)
- Control plane = `core/` (`/api/v1` JSON + WS). Media plane = LiveKit video, direct to browser.
- `RenderBackend` seam: `cloud` (wraps `providers.liveavatar_cloud`) | `self_host` (future). Select via env.
- Portable abstractions: `AppConfig` (env), `SessionStore` (InMemory↔Redis), injected LLM/TTS.

## Status
DONE
- [x] `core/` package: config, store, render seam (base/cloud/mock/self_host), `/api/v1` router, server
- [x] `/api/v1` endpoints: health, lite/start, lite/say, lite/interrupt, lite/stop, ws/control
- [x] CloudRenderBackend reuses tested providers.liveavatar_cloud (no rewrite); mock + self_host prove the seam
- [x] Frontend `lite.html` migrated to `/api/v1`; moved to `frontend/`
- [x] Colab bootstrap notebook (clone→install→weights→move→env→smoke→run→ngrok)
- [x] datasets.yaml + citation validation (16/16 VERIFIED)

IMPLEMENTED (Wave 2)
- [x] Director module: VN bi-encoder clustering, phase-aware scoring, 5-challenge state machine
- [x] `/api/v1/lite/attach` + `/lite/ingest` (Director sync path) + `/lite/chat` (async coordinator)
- [x] TTS engine seam (`core/tts/`): vieneu, cosyvoice, transformers adapters + 6 presets
- [x] LLM engine seam (`core/llm/`): llamacpp, vllm, sglang, hf adapters + streaming
- [x] Mock renderer (`RENDER_BACKEND=mock`): PIL frames with audio-driven mouth, idle loop
- [x] Streaming orchestrator: LLM.stream -> TextChunker -> TTS.stream -> backend.stream_audio
- [x] Auth gates: BACKEND_API_TOKEN, ADMIN_API_TOKEN, WS token validation
- [x] App factory: `create_app(config=None, deps=None)` for testability
- [x] Session locks: per-session non-blocking lock, 409 on concurrent say
- [x] Text chunker: punctuation/max/timeout flush, injectable clock
- [x] EngineManager: runtime swap via `POST /engines/llm`, `/engines/tts`, TTS preset selector
- [x] DirectorCoordinator: background tick loop draining ChatQueue -> Director -> orchestrator
- [x] Continuous MJPEG endpoint (`/mock/video/{id}.mjpeg`) with idle-utterance frame switching
- [x] Qwen3.5-4B GGUF preset

REMAINING
- [ ] Real LLM loader run on Colab GPU: llamacpp GGUF (gemma-3-4b / Qwen3-4B / Qwen3.5-4B Q4_K_M).
- [ ] Self-host diffusion RenderBackend -- research done (see notes 2026-06-22)
- [ ] AWS lift: Redis store, ALB sticky sessions, vLLM + prefix caching, KEDA/DCGM autoscale
- [ ] Production: evaluate Go rewrite of the API/control plane (not for Colab demo)

## Confirmed decisions
- LiveAvatar cloud first; self-host later (multi-image anti-drift, batch-streaming on 1-2 GPUs).
- Traffic-mode: hybrid pre-generated hook pool (gen at session init from shop info, rotate at runtime).
- Barge-in: priority-gated interrupt (Director decides), not unconditional.
- Control plane = WebSocket; media plane = LiveKit/WebRTC.
- LLM: gemma-3-4b-it (Gemma) | Qwen3-4B (Apache) | Qwen3.5-4B (Apache) selectable; TTS: VieNeu-v2 (Apache) default.
- Cluster/retrieval: bkai vietnamese-bi-encoder + vector cosine (no BM25); phase-aware scoring.
- `implementations/` is the workspace root; `providers/liveavatar_cloud` is one render-backend option.

## Verification
```bash
uv run python -m core.tests.v1_smoke_test    # core v1, sandbox
uv run python -m core.tests.v1_smoke_test    # mock mode (no API key)
uv run python -m providers.liveavatar_cloud_cloud.examples.server_ws_smoke_test  # legacy smoke
```
Note: the legacy smoke test path `providers.liveavatar_cloud_cloud` is the old doubled path.
The actual module is `providers.liveavatar_cloud.examples.server_ws_smoke_test`.
