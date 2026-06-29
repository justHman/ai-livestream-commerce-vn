# Backend Production Fix & Scale-Up Plan

## Scope

Review target: `implementations/` backend API server, excluding legacy `liveavatar_demo/`.

Production surface:

```text
implementations/
├── core/              # FastAPI API server, Director, LLM/TTS/render seams
└── liveavatar_api/    # LiveAvatar cloud client/wrapper
```

`liveavatar_demo/` is not part of the production backend path. Keep only if useful as archived reference; otherwise remove/archive after verifying no import/reference from `core/`.

## Key principle

Mock rendering means **mock video/frame rendering only**.

The production/debug pipeline remains:

```text
viewer/chat input
  → Director / API
  → LLM real inference
  → TTS real synthesis/audio
  → RenderBackend
      ├── cloud LiveAvatar
      ├── self-host avatar model
      └── mock-frame renderer
```

So the mock renderer must consume the real TTS output or TTS timing metadata and generate synthetic avatar frames. It must not mock LLM or TTS unless tests explicitly inject fake engines.

## Current blockers

### P0 — No offline render backend

Current backend defaults to cloud/self-host only. Cloud requires `LIVEAVATAR_API_KEY`; self-host is a stub. This blocks offline API tests, frontend stream tests, and CI.

Fix:

- Add `core/render/mock.py`.
- Add `RENDER_BACKEND=mock`.
- Mock renderer returns frontend-safe fake session info and produces synthetic frames/video from real TTS/audio timing.
- Keep LLM/TTS real by default.

Acceptance:

```text
RENDER_BACKEND=mock
LLM_ENGINE=llamacpp
TTS_ENGINE=transformers

/api/v1/lite/start works without LiveAvatar API key
/api/v1/lite/say uses real LLM + real TTS
mock renderer produces synthetic frames for the utterance
```

### P0 — App startup is hard to test

Current app wires dependencies at import/startup. This makes tests sensitive to env, cloud keys, and model loading.

Fix:

- Introduce `create_app(config=None, deps=None)`.
- Keep `app = create_app()` for uvicorn compatibility.
- Allow tests to inject backend/store/engines.
- Split liveness and readiness:
  - `/health/live`: process alive.
  - `/health/ready`: render backend + selected engines loaded.

### P0 — Auth/admin gates missing

Endpoints that cost credits/GPU are currently public if network-exposed.

Fix:

- Add `BACKEND_API_TOKEN` for session/control APIs.
- Add `ADMIN_API_TOKEN` for `/engines/*` and `/debug/*`.
- Add WebSocket auth before `accept()`.
- Gate `/debug/*` behind `DEBUG_ENABLED=1`.
- Reject `CORS_ORIGINS="*"` when `APP_ENV=prod`.

### P1 — Session concurrency unsafe

Concurrent `say`/`interrupt` on the same session can overlap audio/video operations.

Fix:

- Add per-session lock/state.
- MVP: return `409 already_speaking` for concurrent `say`.
- Later: bounded priority queue controlled by Director.

Session states:

```text
idle
speaking
interrupting
stopped
lost
```

### P1 — Redis metadata insufficient for multi-instance

Redis currently does not own enough routing/state info for multi-instance production.

Fix store schema:

```json
{
  "session_id": "...",
  "owner_instance_id": "...",
  "status": "active|speaking|stopped|lost",
  "backend": "cloud|mock|self_host",
  "created_at": "...",
  "expires_at": "..."
}
```

API behavior:

```text
missing store row              → 404
owner_instance_id mismatch     → 409 wrong_instance
store active but backend lost  → 410 session_lost
```

### P1 — TTS warmup bug

`TTSRequest` has no `max_tokens`, but warmup passes it.

Fix:

```python
self.synthesize(TTSRequest(text=text))
```

Add test for `ToneEngine.warmup()`.

### P1 — TTS sample-rate correctness

Transformer TTS must preserve native audio sample rate or actually resample before returning. Do not label audio as 24 kHz unless it is 24 kHz.

Fix:

- Preserve native sample rate in `AudioChunk.sample_rate`.
- If target output rate is configured, resample in adapter before returning.

## RenderBackend contract decision

Two backend archetypes must coexist:

- **Cloud (LiveAvatar)** owns the whole pipeline: caller hands it text, it runs LLM+TTS+avatar internally and returns spoken text. It cannot accept `AudioWindow` objects — its API is turn-level.
- **Mock / self-host** own only the avatar stage: the orchestrator runs LLM and TTS locally and feeds `AudioWindow` objects in, getting `VideoWindow` objects out.

A single `stream_audio(session_id, audio_window)` method on the base ABC would force cloud to fake audio windows it never uses, and forcing cloud onto a separate ABC creates two parallel hierarchies that share `start/stop/interrupt` for no reason.

**Decision:** one base `RenderBackend` ABC with the shared session lifecycle (`start`, `stop`, `interrupt`, plus a new `session_status` for the lock/queue work), and two protocol sub-ABCs:

```text
RenderBackend
  ├── FullPipelineBackend.say(session_id, text, generate=True) -> str
  └── StreamingAvatarBackend.stream_audio(session_id, audio_window) -> Iterator[VideoWindow]
```

The orchestrator (built in the queue-coordinator task) branches on which protocol the configured backend implements. Cloud keeps its current contract unchanged (zero regression); mock and self-host share the `AudioWindow → VideoWindow` contract. `say()` on mock will be removed — mock only supports streaming, which is the production target anyway.

This keeps cloud's turn-level API honest (it does not pretend to stream) and gives mock/self-host a real streaming contract from day one.

## Mock-frame renderer design

### Streaming pipeline target

You want true streaming across all three stages:

```text
LLM stream
  → incremental text units
  → TTS stream
  → incremental audio windows
  → avatar render stream
  → incremental video windows/frames
  → playback queue
```

This is the correct production target.

So the system should not wait for the full LLM response, then full TTS, then full avatar render. Instead:

```text
LLM emits partial text
  → chunker decides safe TTS boundary
  → TTS synthesizes that text chunk
  → avatar backend renders frames for that audio window
  → player starts output while next chunks are being generated
```

### Streaming boundary design

Use explicit chunk objects between stages:

```text
TextChunk
  id
  session_id
  utterance_id
  seq
  text
  is_final

AudioWindow
  id
  session_id
  utterance_id
  seq
  pcm/audio bytes or path
  sample_rate
  duration_ms
  text_span
  is_final

VideoWindow
  id
  session_id
  utterance_id
  seq
  frames
  fps
  duration_ms
  audio_window_id
  is_final
```

### LLM → TTS streaming

Do not send every token directly into TTS. TTS needs stable boundaries.

Use a chunker that flushes on:

```text
- punctuation boundary: . , ! ? ; :
- newline
- phrase length threshold
- timeout threshold if the LLM keeps streaming too long without punctuation
- final token
```

Suggested config:

```env
LLM_STREAM=1
TTS_STREAM=1
AVATAR_STREAM=1
TEXT_CHUNK_MIN_CHARS=12
TEXT_CHUNK_TARGET_CHARS=40
TEXT_CHUNK_MAX_CHARS=80
TEXT_CHUNK_FLUSH_TIMEOUT_MS=350
```

Practical rule:

```text
Too-small chunk  → unnatural TTS / too many render windows
Too-large chunk  → high first-frame latency
```

So target phrase-sized chunks, not token-sized chunks.

### TTS → Avatar streaming

This boundary should be audio-driven.

Rules:

```text
- If TTS provides streaming PCM/audio chunks, use those as the primary input.
- Merge tiny audio chunks until they reach the minimum renderable audio window.
- If TTS returns a full waveform only, split it into audio windows and stream them forward.
```

Recommended config:

```env
AVATAR_AUDIO_WINDOW_MIN_MS=500
AVATAR_AUDIO_WINDOW_TARGET_MS=1000
AVATAR_AUDIO_WINDOW_MAX_MS=2000
AVATAR_LOOKAHEAD_AUDIO_WINDOWS=2
AVATAR_MAX_QUEUE_WINDOWS=5
MOCK_AVATAR_TARGET_FPS=25
```

Frame count per audio window:

```text
num_frames = ceil(audio_window_duration_sec * target_fps)
```

### Mock renderer under streaming

Mock renderer still only mocks the avatar stage.

Pipeline under mock mode:

```text
LLM real stream
  → text chunker
  → TTS real stream or waveform
  → audio windows
  → mock avatar frame generator
  → MJPEG/WebRTC/debug playback
```

Mock renderer behavior per audio window:

```text
- compute duration from audio bytes/sample rate
- compute frame count from FPS
- derive mouth openness from audio RMS/energy envelope
- add blink/head bob/hand wave deterministic loops
- emit VideoWindow immediately when rendered
```

### Required architectural changes for streaming

#### A. LLM interface

Current LLM seam must support incremental generation, not only full response objects.

Target API shape:

```python
for chunk in llm.stream(request):
    yield TextChunk(...)
```

#### B. TTS interface

Current TTS seam must support streamed synthesis.

Target API shape:

```python
for audio_window in tts.stream(text_chunk):
    yield AudioWindow(...)
```

If the backend TTS is non-streaming, wrap it:

```text
synthesize full audio for text chunk
split to AudioWindow sequence
yield incrementally
```

#### C. Render interface (DECISION — see "RenderBackend contract decision" below)

Two backend archetypes, one shared lifecycle base:

```text
RenderBackend (base ABC)
  start(opts) -> StartResult
  stop(session_id) -> None
  interrupt(session_id) -> None
  session_status(session_id) -> SessionStatus   # new: for locking/queue

FullPipelineBackend(RenderBackend)
  say(session_id, text, generate=True) -> str    # cloud: owns LLM+TTS+avatar

StreamingAvatarBackend(RenderBackend)
  stream_audio(session_id, audio_window) -> Iterator[VideoWindow]   # mock/self-host
```

The orchestrator picks the path by protocol the backend implements.

#### D. Queue coordinator

Add a coordinator between stages:

```text
TextChunk queue
AudioWindow queue
VideoWindow queue
```

Responsibilities:

```text
backpressure
lookahead buffering
interrupt/cancel propagation
final-chunk flush
metrics
```

### Interrupt semantics in streaming mode

On interrupt:

```text
1. cancel unfinished LLM stream
2. stop or flush pending TTS generation
3. drop queued AudioWindow and VideoWindow items not yet played
4. stop current playback/render if backend allows
5. mark utterance interrupted
```

### What to implement first

1. LLM streaming in llama.cpp adapter for `Qwen3.5-4B-Q4_K_M.gguf`
2. text chunker between LLM and TTS
3. TTS streaming wrapper interface
4. audio-window abstraction
5. mock avatar renderer consuming `AudioWindow`
6. queue coordinator
7. then adapt self-host avatar model backend to same `AudioWindow → VideoWindow` contract


## LLM enhancement

Add Qwen GGUF preset:

```text
Qwen3.5-4B-Q4_K_M.gguf
```

Source:

```text
https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/blob/main/Qwen3.5-4B-Q4_K_M.gguf
```

Config addition target:

- `core/config.py` LLM env parsing/defaults.
- `core/engine_manager.py` presets.

Suggested preset:

```json
{
  "id": "qwen3.5-4b-gguf-q4-k-m",
  "engine": "llamacpp",
  "model_path": "models/Qwen3.5-4B-Q4_K_M.gguf",
  "n_ctx": 8192,
  "n_gpu_layers": -1,
  "temperature": 0.6,
  "max_tokens": 256,
  "stream": true
}
```

Env override:

```env
LLM_ENGINE=llamacpp
LLM_MODEL_PATH=models/Qwen3.5-4B-Q4_K_M.gguf
LLM_N_CTX=8192
LLM_N_GPU_LAYERS=-1
LLM_STREAM=1
```

Tests:

```text
/engines lists qwen3.5 preset
select qwen3.5 preset updates manager config
missing GGUF path returns clear error
LLM streaming yields incremental text deltas
```

## Implementation order (P0 milestone — streaming-first)

### Task 1 — Streaming data abstractions + audio windowing

Files:

```text
core/render/windows.py
core/tests/test_audio_windowing.py
```

Deliverables:

```text
TextChunk dataclass (id, session_id, utterance_id, seq, text, is_final)
AudioWindow dataclass (id, session_id, utterance_id, seq, pcm or audio_path,
  sample_rate, duration_ms, text_span, is_final)
VideoWindow dataclass (id, session_id, utterance_id, seq, frames, fps,
  duration_ms, audio_window_id, is_final)
audio_windowing helpers:
  split_waveform(pcm, sample_rate, min_ms, target_ms, max_ms) -> list[AudioWindow]
  merge_small_chunks(chunks, min_ms) -> list[AudioWindow]
  num_frames_for(window, fps) -> int   # ceil(duration_sec * fps)
unit tests for split/merge/frame-count edge cases (empty, tiny, exact, last-short)
no dependency on numpy required for the dataclasses; windowing may use stdlib only
```

### Task 2 — LLM streaming interface + Qwen3.5 GGUF preset

Files:

```text
core/llm/base.py        (add stream() to LLMEngine ABC)
core/llm/adapters/llamacpp.py  (implement stream())
core/config.py          (add LLM_STREAM env, qwen3.5 preset)
core/engine_manager.py  (register qwen3.5 preset)
core/tests/test_llm_streaming.py
```

Deliverables:

```text
LLMEngine.stream(request) -> Iterator[TextChunk]   (new ABC method)
llama.cpp adapter streaming via the adapter's incremental generate API
Qwen3.5-4B-Q4_K_M.gguf preset registered in engine_manager
LLM_STREAM=1 env parsed in config
missing GGUF path raises a clear RuntimeError naming the expected path
unit test: streaming yields >=2 TextChunks for a multi-sentence prompt
  (use a fake/stub llm engine that emits fixed deltas if real model load
   is not available offline — but the ABC + llama.cpp stream path must be
   exercised; gate the real-GGUF test behind an integration marker)
```

### Task 3 — Text chunker (LLM → TTS boundary)

Files:

```text
core/stream/chunker.py
core/tests/test_text_chunker.py
```

Deliverables:

```text
TextChunker flushes on: punctuation (. , ! ? ; :), newline,
  phrase length threshold, flush timeout, final token
config: TEXT_CHUNK_MIN_CHARS=12, TEXT_CHUNK_TARGET_CHARS=40,
  TEXT_CHUNK_MAX_CHARS=80, TEXT_CHUNK_FLUSH_TIMEOUT_MS=350
chunker.feed(token_text) accumulates; chunker.flush() emits TextChunk(s)
chunker takes a clock callable so timeout is deterministic in tests
unit tests: punctuation flush, max-chars flush, timeout flush, final flush,
  min-chars coalescing (does not emit chunks shorter than min unless final)
```

### Task 4 — TTS streaming interface + wrapper

Files:

```text
core/tts/base.py        (add stream() to TTSEngine ABC)
core/tts/adapters/*.py  (stream() — wrap existing synthesize for non-streaming)
core/tests/test_tts_streaming.py
```

Deliverables:

```text
TTSEngine.stream(text_chunk) -> Iterator[AudioWindow]   (new ABC method)
non-streaming adapter impl: synthesize full audio for chunk, then use
  audio_windowing.split_waveform to emit AudioWindows incrementally
fix existing TTS warmup bug (TTSRequest has no max_tokens) in the same pass
  — change to self.synthesize(TTSRequest(text=text))
preserve native sample rate in AudioChunk/AudioWindow; do not label as 24kHz
  unless actually 24kHz
unit test: stream() of a stub TTS returns multiple AudioWindows whose
  durations sum to the full chunk duration
unit test: ToneEngine.warmup() no longer swallows TypeError
```

### Task 5 — Mock avatar renderer (streaming)

Files:

```text
core/render/mock.py
core/render/__init__.py
core/config.py           (RENDER_BACKEND=mock, mock config block)
core/api/v1.py           (wire mock backend, mock media endpoints)
core/tests/test_mock_render_lifecycle.py
core/tests/test_mock_frame_generation.py
```

Deliverables:

```text
MockRenderBackend(StreamingAvatarBackend):
  start() -> StartResult (deterministic fake session_id, livekit_url, token, mode="MOCK")
  stream_audio(session_id, audio_window) -> Iterator[VideoWindow]
    - compute duration from audio_window.sample_rate + bytes
    - num_frames = ceil(duration_sec * MOCK_AVATAR_TARGET_FPS)
    - synthesize frames: mouth open from audio RMS, blink, head bob, hand wave,
      text overlay (session_id, utterance_id, frame_idx, timestamp, text_span)
    - yield one VideoWindow per AudioWindow
  interrupt(session_id), stop(session_id) — per-session state, KeyError on unknown
  session_status(session_id) -> SessionStatus
endpoints:
  GET /api/v1/mock/frame/{session_id}.png
  GET /api/v1/mock/video/{session_id}.mjpeg
  GET /api/v1/mock/status/{session_id}
config: MOCK_AVATAR_TARGET_FPS=25, MOCK_AVATAR_WIDTH=640, MOCK_AVATAR_HEIGHT=360
RENDER_BACKEND=mock selectable in AppConfig.build_render_backend()
importing core.server must not require LIVEAVATAR_API_KEY when backend=mock
unit tests: lifecycle (start/stream/stop, unknown session raises KeyError),
  frame PNG decodes and matches configured dimensions, frames differ over time
```

### Task 6 — App factory + offline test infrastructure

Files:

```text
core/server.py           (create_app(config=None, deps=None))
core/api/v1.py           (init_deps retained; accept injected deps)
core/tests/test_app_factory.py
```

Deliverables:

```text
create_app(config=None, deps=None) -> FastAPI
  - builds config from env if not provided
  - accepts injected RenderBackend / store / engines for tests
module-level app = create_app() kept for uvicorn
/health/live (process alive) and /health/ready (backend + engines ready) split
importing core.server does NOT load heavy models when RENDER_BACKEND=mock
  and LLM_ENGINE/TTS_ENGINE are stub/tone
offline pytest runs without LIVEAVATAR_API_KEY, REDIS_URL, or GPU
unit test: create_app with MockRenderBackend + stub engines boots, /health/live 200
```

### Task 7 — Auth / admin / debug gates

Files:

```text
core/api/auth.py
core/api/v1.py
core/server.py
core/config.py
core/tests/test_api_auth.py
core/tests/test_ws_auth.py
```

Deliverables:

```text
BACKEND_API_TOKEN dependency for /lite/* and /ws/control
ADMIN_API_TOKEN dependency for /engines/* and /debug/*
WebSocket token validated before accept()
/debug/* gated behind DEBUG_ENABLED=1 (else 404)
reject CORS_ORIGINS="*" when APP_ENV=prod (startup error)
in APP_ENV=dev with no tokens set, auth is disabled (so local dev + tests work)
unit tests: missing token -> 401, viewer token on /engines -> 403,
  valid admin token -> 200, ws without token -> closes before accept,
  DEBUG_ENABLED=0 -> /debug/* -> 404
```

### Task 8 — Session lock + queue coordinator

Files:

```text
core/render/locks.py     (per-session lock registry)
core/render/queue.py     (TextChunk/AudioWindow/VideoWindow queues + coordinator)
core/render/orchestrator.py  (LLM stream -> chunker -> TTS stream -> backend stream)
core/api/v1.py           (wire coordinator into /lite/say; 409 on concurrent say)
core/tests/test_session_concurrency.py
core/tests/test_queue_coordinator.py
```

Deliverables:

```text
per-session asyncio.Lock; concurrent /lite/say on a speaking session -> 409 already_speaking
interrupt cancels: LLM stream, pending TTS, queued Audio/VideoWindows not yet played
coordinator wires: LLM.stream -> TextChunker -> TTS.stream -> backend.stream_audio
  (branches on FullPipelineBackend vs StreamingAvatarBackend)
bounded VideoWindow queue with AVATAR_MAX_QUEUE_WINDOWS; drop-oldest or backpressure
metrics: pipeline_total_ms, queue_depth_windows, dropped_windows
unit tests: two concurrent says -> one 409, interrupt mid-stream stops emission,
  coordinator end-to-end with stub LLM+TTS+mock backend emits ordered VideoWindows
```

### Deferred (post-P0)

```text
Redis production state (owner_instance_id, TTL, 409/410) — Step 5
Self-host renderer adapter (MuseTalk/Ditto/LLIA) — Step 6
Integration tests (cloud, redis, gguf, transformers) — test plan integration section
```

## Test plan

Offline:

```text
test_mock_render_lifecycle.py
test_mock_frame_generation.py
test_api_lite_mock_real_engines.py
test_api_auth.py
test_ws_auth.py
test_director_ingest_mock.py
test_session_concurrency.py
test_tts_warmup.py
test_audio_windowing.py
```

Integration:

```text
test_liveavatar_cloud_integration.py     # needs LIVEAVATAR_API_KEY
test_redis_store_integration.py          # needs REDIS_URL
test_llamacpp_qwen_gguf_integration.py   # needs GGUF path + GPU/CPU support
test_tts_transformers_integration.py     # needs model cache/GPU optional
```

## Definition of done

```text
Backend can run with RENDER_BACKEND=mock and real LLM/TTS.
No LiveAvatar API key required for offline test/demo.
Mock renderer generates valid image/video frames aligned to audio windows.
Qwen3.5 4B GGUF preset appears in engines and has clear missing-file error.
Public endpoints require auth in non-dev mode.
Concurrent say/interrupt behavior deterministic.
Pytest offline suite passes.
```
