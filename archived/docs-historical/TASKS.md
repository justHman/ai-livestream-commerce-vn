# TASKS.md -- Historical

> This doc is historical. See `architecture.md` for the current status.
>
> Key changes since this was written:
> - `liveavatar_api/` -> `providers/liveavatar_cloud/`
> - `providers/liveavatar_cloud_cloud/` -> `providers/liveavatar_cloud/`
> - `liveavatar_demo/` -> `archive/legacy-liveavatar-demo/`
> - Frontend in `liveavatar_api/frontend/` -> `frontend/`
> - Many items marked "NEXT" are now DONE (see Wave 2 items below)

# TASKS — VN Live-Commerce Host (implementations/)

## Done (initial refactor)
- [x] `core/config.py` — AppConfig (env) + RENDER_BACKEND / SESSION_STORE selectors
- [x] `core/store.py` — SessionStore: InMemory (Colab) | Redis (AWS)
- [x] `core/render/base.py` — RenderBackend ABC + StartResult + FullPipelineBackend + StreamingAvatarBackend
- [x] `core/render/cloud.py` — CloudRenderBackend wrapping providers.liveavatar_cloud
- [x] `core/render/self_host.py` — stub (NotImplemented), proves the seam
- [x] `core/render/mock.py` — MockRenderBackend (PIL frames, audio-driven mouth, idle loop, MJPEG)
- [x] `core/api/v1.py` — /api/v1 routes + WS control hub
- [x] `core/api/auth.py` — viewer_auth, admin_auth, debug_enabled_dep, validate_ws_token
- [x] `core/server.py` — create_app() factory + module-level app for uvicorn
- [x] `core/tests/v1_smoke_test.py` — sandbox v1 smoke
- [x] Migrate `frontend/lite.html` to `/api/v1`; move to `frontend/` dir
- [x] `notebooks/bootstrap_colab.ipynb` — clone->install->weights->move->env->smoke->run->ngrok
- [x] `datasets.yaml` + `notes/validation-report-2026-06-22.md` (16/16 VERIFIED)
- [x] Verify: core v1 + legacy smoke pass on sandbox; self_host NotImplemented confirmed

## Wave 2 (Director + streaming + infrastructure)
- [x] Director module (`core/director/`): bi-encoder clustering, phase scoring, 5-challenge FSM,
      StreamConfig, traffic-mode hook pool, two-tier retrieval (TIER1 semantic + TIER2 O(1) field lookup)
- [x] Wire Director into the say-loop: `/lite/attach` + `/lite/ingest` (comments -> Decision -> speak)
- [x] Model-agnostic TTS seam (`core/tts/`): TTSEngine ABC + registry + adapters
      (vieneu/cosyvoice/transformers) + ToneEngine stub. 6 presets registered.
- [x] Model-agnostic LLM seam (`core/llm/`): LLMEngine ABC + adapters (llamacpp/vllm/sglang/hf)
      + streaming support via `stream_chunks()`
- [x] Text chunker (`core/stream/chunker.py`): punctuation/max/timeout flush, injectable clock
- [x] Streaming data types (`core/render/windows.py`): TextChunk, AudioWindow, VideoWindow + helpers
- [x] BoundedVideoQueue (`core/render/queue.py`): drop-oldest on overflow + CoordinatorMetrics
- [x] Session locks (`core/render/locks.py`): per-session non-blocking try_acquire, 409 on concurrent say
- [x] Streaming orchestrator (`core/render/orchestrator.py`): LLM->chunker->TTS->backend in one thread
- [x] DirectorCoordinator (`core/director/coordinator.py`): background tick loop draining ChatQueue
- [x] ChatQueue (`core/director/chat_queue.py`): per-session rolling comment window
- [x] EngineManager (`core/engine_manager.py`): runtime swap via POST /engines/llm, /engines/tts
- [x] TTS preset selector (`POST /engines/tts/preset`): frontend dropdown -> apply preset config
- [x] Continuous MJPEG endpoint (`/mock/video/{id}.mjpeg`): frames from queue or idle loop
- [x] Qwen3.5-4B GGUF preset registered
- [x] `/lite/chat` endpoint: single comment -> coordinator queue (202 Accepted)

## Remaining
- [ ] VERIFY on GPU: confirm VieNeu official runtime import/call against the model card
- [ ] Real LLM loader run on Colab GPU: llamacpp GGUF (gemma-3-4b / Qwen3-4B / Qwen3.5-4B Q4_K_M)
- [ ] Self-host diffusion RenderBackend — research done, model TBD:
        #1 Live Avatar (Quark-Vision/Live-Avatar, Apache-2.0) = open-source of the cloud renderer;
           multi-ref + AR-infinite + anti-drift, but needs >=48GB (FP8) / 2xA100 pooled
        fallback Ditto (Apache, ~0.2B, RTF<1 on 1 GPU) — single-ref, weaker anti-drift
        API-only/closed (cannot self-host): OmniHuman-1, EMO/EMO2, Loopy
        lip-sync is language-agnostic (audio-driven) -> no VN finetune needed
- [ ] Optional: DBSCAN/HDBSCAN clustering for post-stream analysis
- [ ] AWS lift: Redis store, ALB sticky sessions, vLLM + prefix caching, KEDA/DCGM autoscale
- [ ] Evaluate Go rewrite of API/control plane (not for Colab demo)

## Notes
- Keep `providers/liveavatar_cloud/backend/*` untouched so its standalone smoke tests stay green
- All dev on sandbox avatar (0 credits); free tier has 10 credits total
- Secrets backend-only; browser gets only livekit_url + livekit_client_token
- `archive/legacy-liveavatar-demo/` is archived, not in the production path
