# tts compatibility shim — `imjusthman/ai-live-tts`

Canonical source and build: `services/product/tts_service/`.

vLLM-Omni / VieNeu TTS on the **same g6 Task** as LLM (GPU share).

| Item | Value |
|------|-------|
| Port | `8002` |
| Health | `GET /health` |
| Arch | `linux/amd64` + NVIDIA GPU |
| Default model | `pnnbao-ump/VieNeu-TTS-v2` |
| Weights | S3 via `WEIGHTS_S3_URI` → `/models` |

## Build

```bash
docker build -f services/tts/Dockerfile -t imjusthman/ai-live-tts:dev .
```

## Run

```bash
docker run --rm --gpus all -p 8002:8002 \
  -e WEIGHTS_S3_URI=s3://ai-livestream-dev/weights/tts/ \
  -e MODEL_ID=pnnbao-ump/VieNeu-TTS-v2 \
  -e GPU_MEMORY_UTILIZATION=0.25 \
  -e NVIDIA_VISIBLE_DEVICES=all \
  imjusthman/ai-live-tts:dev
```

## ECS notes

- Does **not** declare GPU resource; shares LLM device via `NVIDIA_VISIBLE_DEVICES`.
- Target util `0.25` so LLM can hold `0.6` on 24GB L4.
- Replace `vllm serve` with Omni fork entry once wheel is published.
