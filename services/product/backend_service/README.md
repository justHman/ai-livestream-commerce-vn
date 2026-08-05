# backend — `imjusthman/ai-live-backend`

FastAPI control plane for Fargate Spot **ARM64**.

| Item | Value |
|------|-------|
| Port | `8800` |
| Health live | `GET /api/v1/health/live` |
| Health ready | `GET /api/v1/health/ready` |
| Arch | `linux/arm64` (Graviton) |
| Weights | none |

## Build

From the repository root. Dependencies resolve from this service's `pyproject.toml` and `uv.lock`; sibling llm/tts/avatar srcs are copied by the Dockerfile:

```bash
docker build -f services/product/backend_service/Dockerfile -t imjusthman/ai-live-backend:dev .
```

## Run

```bash
docker run --rm -p 8800:8800 \
  -e APP_ENV=dev \
  -e LLM_BASE_URL=http://llm:8001 \
  -e TTS_BASE_URL=http://tts:8002 \
  -e AVATAR_BASE_URL=http://avatar:8080 \
  -e LIVEKIT_URL=ws://livekit:7880 \
  -e REDIS_URL=redis://redis:6379/0 \
  -e DATABASE_URL=postgresql://... \
  imjusthman/ai-live-backend:dev
```

## Notes

- Multi-stage: deps install → slim runtime, non-root `appuser`.
- CMD: `uvicorn backend.main:app --host 0.0.0.0 --port 8800`.
- Prefer building with `--platform linux/arm64` for prod Fargate ARM.
