# tts — `imjusthman/ai-live-tts`

Provider-neutral self-host TTS serving (FastAPI control plane + scheduler).

| Item | Value |
|------|-------|
| Port | `8002` |
| Health | `GET /health` (liveness), `GET /ready` (readiness) |
| Metrics | `GET /v1/audio/metrics` (JSON snapshot) |
| Arch | `linux/amd64` + NVIDIA GPU (or CPU-only) |
| Default provider | `vieneu_v3` — `pnnbao-ump/VieNeu-TTS-v3-Turbo` |
| Weights | S3 via `WEIGHTS_S3_URI` → `/models` (optional; never baked) |

## Serving model

The service is **provider-neutral**: routes speak `TTSProvider` (synthesis +
voice enrollment), the scheduler drives one provider lane, and the legacy
engine path remains as fallback when the provider is not ready. The default
provider is VieNeu v3 Turbo (PyTorch/GPU batched; ONNX/CPU single-path
fallback). See `src/tts/providers/` for the provider seam.

## Build

```bash
# CPU-only image (ONNX backend)
docker build -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .

# GPU image (VieNeu pytorch backend — torch CUDA wheels)
docker build --build-arg WITH_CUDA=1 -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .
```

CI builds (`container-build`) use a per-service gha cache scope (`tts`) so
develop/main merge builds reuse layers from the feature-PR build instead of
rebuilding from scratch. Dockerfile layer edits invalidate only the changed
layers onward.

NVIDIA runtime requirements (GPU mode):

- Host must run `nvidia-container-toolkit` (the image carries no CUDA base).
- Container must request the GPU: `--gpus all` / ECS `resourceRequirements`
  GPU=1, plus `NVIDIA_VISIBLE_DEVICES` (default `all`).

## Run

```bash
docker run --rm --gpus all -p 8002:8002 \
  -e TTS_PROVIDER=vieneu_v3 \
  -e TTS_MODEL_REVISION=pnnbao-ump/VieNeu-TTS-v3-Turbo \
  -e TTS_ACCELERATOR=auto \
  -e WEIGHTS_S3_URI=s3://ai-livestream-dev/weights/tts/ \
  imjusthman/ai-live-tts:dev
```

CPU-only local/test boot (no GPU host needed):

```bash
docker run --rm -p 8002:8002 \
  -e TTS_ACCELERATOR=cpu \
  imjusthman/ai-live-tts:dev
```

## API surface

- `POST /v1/audio/speech` (alias `POST /v1/speech`) — synthesize one chunk
  with tracing headers (`X-Request-Id`, `X-Session-Id`, `X-Utterance-Id`,
  `X-Chunk-Seq`).
- `GET /v1/audio/capabilities` — provider/model/backend capability facts.
- `GET /v1/audio/metrics` — bounded-label counters/gauges/histograms JSON.
- `POST /v1/voices` — enroll a cloned voice (raw WAV body; preset seeding via
  `preset=true`). Profiles are tenant-scoped via `X-Tenant-Id`.
- `GET /v1/voices`, `DELETE /v1/voices/{id}` — list/delete profiles.

## Voice profiles

Enrollment validates reference audio bounds (`TTS_VOICE_MAX_BYTES` /
`TTS_VOICE_MAX_SECONDS`), encodes the provider payload once, and persists it
to the store (`file://` or `s3://` URI via `TTS_VOICE_STORE_URI`). A bounded
LRU cache serves hot profiles; provider payloads never cross the API.

## Scheduler

Continuous dynamic micro-batching (`src/tts/scheduler/`):

- **Coalescing** — `TTS_COALESCE_WINDOW_MS` (default 10 ms) before dispatch;
  immediate dispatch when a batch fills or a request deadline approaches.
- **Fairness** — per-session FIFO + deficit round robin, high-priority tier
  first, aging protection (`TTS_AGING_THRESHOLD_MS`).
- **Backpressure** — admission bounds `TTS_GLOBAL_PENDING_LIMIT` (512) and
  `TTS_PER_SESSION_PENDING_LIMIT` (64); excess is rejected with 429.
- **Deadlines** — `TTS_REQUEST_DEADLINE_MS` (30 s) sweeps overdue requests.
- **Batching** — `TTS_MAX_BATCH_SIZE` (32) native-batch bound; CPU/non-batch
  providers force batch size 1.

## Benchmarks

- `scripts/benchmark_provider.py` — direct provider corpus sweep
  (batch sizes 1/4/8/16/32, same-voice and mixed-voice corpora, RTF +
  items/sec).
- `scripts/benchmark_multisession.py` — concurrent `/v1/audio/speech`
  sessions (1-32) measuring batch fill and service overhead.

## Environment

| Var | Default | Meaning |
|-----|---------|---------|
| `TTS_PROVIDER` | `vieneu_v3` | Provider name (`none` disables the runtime) |
| `TTS_MODEL_REVISION` | `pnnbao-ump/VieNeu-TTS-v3-Turbo` | Model revision for the provider |
| `TTS_ACCELERATOR` | `auto` | `auto` / `cpu` / `gpu` |
| `TTS_RESPONSE_FORMAT` | `wav` | `pcm` / `wav` |
| `TTS_GLOBAL_PENDING_LIMIT` | `512` | Admission cap for pending requests |
| `TTS_PER_SESSION_PENDING_LIMIT` | `64` | Per-session pending cap |
| `TTS_REQUEST_DEADLINE_MS` | `30000` | Deadline sweep bound |
| `TTS_MAX_BATCH_SIZE` | `32` | Service batch ceiling |
| `TTS_COALESCE_WINDOW_MS` | `10` | Coalescing window |
| `TTS_AGING_THRESHOLD_MS` | `5000` | Aging promotion threshold |
| `TTS_VOICE_STORE_URI` | `file://.runtime/voice_profiles` | Voice profile store |
| `TTS_VOICE_MAX_BYTES` | `10485760` | Enrollment reference WAV bound |
| `TTS_VOICE_MAX_SECONDS` | `30` | Enrollment reference duration bound |
| `PORT` | `8002` | HTTP port |
| `WEIGHTS_S3_URI` | *(empty)* | Optional S3 weights sync prefix |
| `NVIDIA_VISIBLE_DEVICES` | `all` | GPU devices for the container runtime |
| `LOG_LEVEL` | `INFO` | Service log level |
