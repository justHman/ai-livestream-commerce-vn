# services/ — Docker images (code + deps only)

Hub namespace: `imjusthman/ai-live-{svc}`. Weights on S3, never in layers.

| Dir | Image | Port | Arch |
|-----|-------|------|------|
| `product/backend_service/` | `imjusthman/ai-live-backend` | 8800 | arm64 |
| `product/llm_service/` | `imjusthman/ai-live-llm` | 8001 | amd64+GPU |
| `product/tts_service/` | `imjusthman/ai-live-tts` | 8002 | amd64+GPU |
| `product/avatar_service/` | `imjusthman/ai-live-avatar` | 8080 | amd64+GPU |
| `platform/livekit/` | `imjusthman/ai-live-livekit` | 7880 + UDP 50000-60000 | arm64 |
| `platform/lmcache/` | `imjusthman/ai-live-lmcache` | 5555 + 8080 | arm64 |
| `llm-tts/` | (Task family docs only) | — | — |
| `scripts/` | `fetch_weights.sh` shared helper | — | — |

## Build (from repo root)

```bash
docker build -f services/product/backend_service/Dockerfile -t imjusthman/ai-live-backend:dev .
docker build -f services/product/llm_service/Dockerfile -t imjusthman/ai-live-llm:dev .
docker build -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .
docker build -f services/product/avatar_service/Dockerfile -t imjusthman/ai-live-avatar:dev .
docker build -f services/platform/livekit/Dockerfile -t imjusthman/ai-live-livekit:dev .
docker build -f services/platform/lmcache/Dockerfile -t imjusthman/ai-live-lmcache:dev .
```

## Weight entrypoint contract

GPU services (`llm`, `tts`, `avatar`) call `services/scripts/fetch_weights.sh` when:

```
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/{svc}/
WEIGHTS_LOCAL_DIR=/models   # optional
```

Task role must allow `s3:GetObject` + `s3:ListBucket` on that prefix. Image stays ~code+deps.

## Ignore rules

- Each canonical built Dockerfile has adjacent `Dockerfile.dockerignore` for its root context.
- Repo root `.dockerignore` remains the conservative fallback.
- `services/.dockerignore` applies only to explicit legacy compatibility builds.
