# PLAN — VN Live-Commerce Host (implementations/)

## Goal
A production-standard, portable live-commerce avatar stack: versioned `/api/v1`, a swappable
renderer (LiveAvatar cloud now, self-host diffusion later), Colab→AWS portability via env, a
Colab bootstrap notebook, and a backend-agnostic Director (next phase).

## Architecture (see PRODUCTION.md)
- Control plane = `core/` (`/api/v1` JSON + WS). Media plane = LiveKit video, direct to browser.
- `RenderBackend` seam: `cloud` (wraps `liveavatar_api`) | `self_host` (future). Select via env.
- Portable abstractions: `AppConfig` (env), `SessionStore` (InMemory↔Redis), injected LLM/TTS.

## Status
DONE
- [x] `core/` package: config, store, render seam (base/cloud/self_host), `/api/v1` router, server
- [x] `/api/v1` endpoints: health, lite/start, lite/say, lite/interrupt, lite/stop, ws/control
- [x] CloudRenderBackend reuses tested liveavatar_api (no rewrite); self_host stub proves the seam
- [x] Frontend `lite.html` migrated to `/api/v1`; `colab_deploy.py` serves `core.server:app`
- [x] Colab bootstrap notebook (clone→install→weights→move→env→smoke→run→ngrok)
- [x] datasets.yaml + citation validation (16/16 VERIFIED); PRODUCTION/PLAN/TASKS docs
- [x] Sandbox smoke: core v1 + legacy both pass; self_host NotImplemented confirmed

NEXT (separate phase)
- [ ] Director: VN bi-encoder clustering, phase-aware scoring, 5-challenge state machine
- [ ] Real model loaders in `colab_deploy` (llama.cpp GGUF LLM + VieNeu-TTS) on a GPU run
- [ ] Self-host diffusion renderer (model TBD — research agent), multi-image anti-drift
- [ ] AWS lift: Redis store, ALB sticky sessions, vLLM + prefix cache, KEDA autoscale

## Confirmed decisions
- LiveAvatar cloud first; self-host later (multi-image anti-drift, batch-streaming on 1-2 GPUs).
- Traffic-mode: hybrid pre-generated hook pool (gen at session init from shop info, rotate at runtime).
- Barge-in: priority-gated interrupt (Director decides), not unconditional.
- Control plane = WebSocket; media plane = LiveKit/WebRTC.
- LLM: gemma-3-4b-it (Gemma) | Qwen3-4B (Apache) selectable; TTS: VieNeu-v2 (Apache) default.
- Cluster/retrieval: bkai vietnamese-bi-encoder + vector cosine (no BM25); phase-aware scoring.
- `implementations/` is the workspace root; `liveavatar_api` is one render-backend option.

## Verification
`python -m core.tests.v1_smoke_test` (core v1, sandbox) and
`python -m liveavatar_api.examples.server_ws_smoke_test` (legacy) — both pass on sandbox, 0 credits.
