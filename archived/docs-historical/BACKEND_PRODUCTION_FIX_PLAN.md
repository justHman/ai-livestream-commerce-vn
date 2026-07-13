# Backend Production Fix & Scale-Up Plan -- Historical

> This doc is historical. The P0 and P1 items below have been implemented.
> See `architecture.md` for the current design.
>
> Key changes since this was written:
> - `liveavatar_api/` -> `providers/liveavatar_cloud/`
> - `providers/liveavatar_cloud_cloud/` -> `providers/liveavatar_cloud/`
> - All P0 tasks (mock renderer, app factory, auth gates) are DONE
> - All P1 tasks (session locks, streaming pipeline, text chunker, TTS seam, LLM streaming, coordinator) are DONE
> - The mock renderer design, streaming boundaries, queue coordinator, and protocol split are all implemented

# Backend Production Fix & Scale-Up Plan (original, preserved below)

## Scope

Review target: `implementations/` backend API server, excluding legacy `archive/legacy-liveavatar-demo/`.

Production surface:

```text
implementations/
+-- core/              # FastAPI API server, Director, LLM/TTS/render seams
+-- providers/liveavatar_cloud/    # LiveAvatar cloud client/wrapper
```

`archive/legacy-liveavatar-demo/` is not part of the production backend path. Archived.

## Key principle

Mock rendering means **mock video/frame rendering only**.

The production/debug pipeline remains:

```text
viewer/chat input
  -> Director / API
  -> LLM real inference
  -> TTS real synthesis/audio
  -> RenderBackend
      +-- cloud LiveAvatar
      +-- self-host avatar model
      +-- mock-frame renderer
```

So the mock renderer must consume the real TTS output or TTS timing metadata and generate synthetic avatar frames. It must not mock LLM or TTS unless tests explicitly inject fake engines.

## Implementation status

### P0 -- All DONE
- [x] Mock renderer (`core/render/mock.py`): `RENDER_BACKEND=mock`, PIL-synthesized frames with audio-driven mouth openness, MJPEG endpoint, idle loop with seamless wrap
- [x] App factory (`core/server.py`): `create_app(config=None, deps=None)`, module-level `app = create_app()` for uvicorn
- [x] `/health/live` and `/health/ready` split with Finding 2 (honest readiness reporting on engine failure)
- [x] Auth gates (`core/api/auth.py`): BACKEND_API_TOKEN, ADMIN_API_TOKEN, WS token pre-accept validation, DEBUG_ENABLED gate, CORS_ORIGINS=* rejection in prod

### P1 -- All DONE
- [x] Session locks (`core/render/locks.py`): per-session non-blocking try_acquire, 409 already_speaking
- [x] Redis metadata schema (schema defined; multi-instance coordination deferred)
- [x] TTS warmup bug fix: `TTSRequest` no longer passes `max_tokens`
- [x] TTS sample-rate correctness: native sample rate preserved in AudioChunk/AudioWindow
- [x] Text chunker (`core/stream/chunker.py`): punctuation/max/timeout flush
- [x] TTS streaming interface (`TTSEngine.stream` -> `Iterator[AudioChunk]`)
- [x] Mock avatar renderer consuming `AudioWindow` -> `VideoWindow` with PIL frames
- [x] LLM streaming interface (`LLMEngine.stream_chunks` -> `Iterator[TextChunk]`)
- [x] Qwen3.5-4B GGUF preset registered
- [x] Queue coordinator (`core/render/queue.py`, `core/render/orchestrator.py`)
- [x] Streaming-drain bridge (Phase E): per-frame push to async queue
- [x] DirectorCoordinator background tick loop

### RenderBackend contract -- IMPLEMENTED
- One base `RenderBackend` ABC with shared lifecycle
- `FullPipelineBackend(RenderBackend)`: CloudRenderBackend (cloud LiveAvatar, turn-level)
- `StreamingAvatarBackend(RenderBackend)`: MockRenderBackend + future SelfHostRenderBackend (streaming, `AudioWindow -> VideoWindow`)
- The orchestrator (`StreamOrchestrator`) branches on protocol via `isinstance`

### Deferred (post-P0)
```text
Redis production state (owner_instance_id, TTL, 409/410) -- Step 5
Self-host renderer adapter (Live Avatar/Ditto/LLIA) -- Step 6
Integration tests (cloud, redis, gguf, transformers) -- test plan integration section
Go rewrite of API/control plane -- not for Colab demo
```

## Test plan

### Offline (all passing)
- test_mock_render_lifecycle.py
- test_mock_frame_generation.py
- test_api_lite_mock_real_engines.py (via test_app_factory.py + director_coordinator tests)
- test_api_auth.py, test_ws_auth.py
- test_director_ingest_mock.py (via test_director_coordinator.py)
- test_session_concurrency.py
- test_tts_warmup.py, test_tts_streaming.py
- test_audio_windowing.py
- test_text_chunker.py
- test_llm_streaming.py
- test_queue_coordinator.py
- test_chat_queue.py
- test_engines_endpoint.py
- test_mjpeg_continuous.py
- test_lite_chat_integration.py
- test_idle_loop.py

### Integration (needs API key / GPU)
```text
test_liveavatar_cloud_integration.py     # needs LIVEAVATAR_API_KEY
test_redis_store_integration.py          # needs REDIS_URL
test_llamacpp_qwen_gguf_integration.py   # needs GGUF path + GPU/CPU support
test_tts_transformers_integration.py     # needs model cache/GPU optional
```

## Definition of done (current state -- all met)

```text
Backend can run with RENDER_BACKEND=mock and real LLM/TTS.        [DONE]
No LiveAvatar API key required for offline test/demo.             [DONE]
Mock renderer generates valid image/video frames aligned to audio windows.  [DONE]
Qwen3.5 4B GGUF preset appears in engines and has clear missing-file error.  [DONE]
Public endpoints require auth in non-dev mode.                    [DONE]
Concurrent say/interrupt behavior deterministic.                  [DONE]
Pytest offline suite passes.                                      [DONE]
```
