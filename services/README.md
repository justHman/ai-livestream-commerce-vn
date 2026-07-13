# services/ — Docker images (code + deps only)

Hub namespace: `justhman/ai-live-{svc}`. Weights on S3, never in layers.

| Dir | Image | Port | Arch |
|-----|-------|------|------|
| `backend/` | `justhman/ai-live-backend` | 8800 | arm64 |
| `llm/` | `justhman/ai-live-llm` | 8001 | amd64+GPU |
| `tts/` | `justhman/ai-live-tts` | 8002 | amd64+GPU |
| `avatar/` | `justhman/ai-live-avatar` | 8080 | amd64+GPU |
| `livekit/` | `justhman/ai-live-livekit` | 7880 + UDP 50000-60000 | arm64 |
| `lmcache/` | `justhman/ai-live-lmcache` | 5555 + 8080 | arm64 |
| `llm-tts/` | (Task family docs only) | — | — |
| `scripts/` | `fetch_weights.sh` shared helper | — | — |

## Build (from repo root)

```bash
docker build -f services/backend/Dockerfile -t justhman/ai-live-backend:dev .
docker build -f services/llm/Dockerfile -t justhman/ai-live-llm:dev .
docker build -f services/tts/Dockerfile -t justhman/ai-live-tts:dev .
docker build -f services/avatar/Dockerfile -t justhman/ai-live-avatar:dev .
docker build -f services/livekit/Dockerfile -t justhman/ai-live-livekit:dev .
docker build -f services/lmcache/Dockerfile -t justhman/ai-live-lmcache:dev .
```

## Weight entrypoint contract

GPU services (`llm`, `tts`, `avatar`) call `services/scripts/fetch_weights.sh` when:

```
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/{svc}/
WEIGHTS_LOCAL_DIR=/models   # optional
```

Task role must allow `s3:GetObject` + `s3:ListBucket` on that prefix. Image stays ~code+deps.

## Ignore rules

- Repo root `.dockerignore` — used for root-context builds.
- `services/.dockerignore` — fallback if context is `services/`.
