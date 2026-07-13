# llm — `justhman/ai-live-llm`

vLLM OpenAI-compatible server on **g6.xlarge L4** (shared ECS Task with TTS).

| Item | Value |
|------|-------|
| Port | `8001` |
| Health | `GET /health` |
| Arch | `linux/amd64` + NVIDIA GPU |
| Default model | `cyankiwi/Qwen3.5-4B-AWQ-4bit` |
| Weights | S3 via `WEIGHTS_S3_URI` → `/models` |

## Build

```bash
docker build -f services/llm/Dockerfile -t justhman/ai-live-llm:dev .
```

Requires a CUDA-capable build host (or multi-stage remote builder) for a full vLLM install.

## Run

```bash
docker run --rm --gpus all -p 8001:8001 \
  -e WEIGHTS_S3_URI=s3://ai-livestream-dev/weights/llm/ \
  -e MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit \
  -e GPU_MEMORY_UTILIZATION=0.6 \
  -e ENABLE_PREFIX_CACHING=1 \
  justhman/ai-live-llm:dev
```

## ECS notes

- Same Task as TTS; **only this container** declares GPU resource (0.6 util).
- Task role needs `s3:GetObject` + `s3:ListBucket` on weights prefix.
- Image = code + deps only. Weights never `COPY`'d.
