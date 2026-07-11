# Service contracts — AWS multi-service stack

> Phase 0 freeze. Source: `plans/02-master-implement-roadmap.md` §5.  
> Region: `ap-northeast-2`. Images: Docker Hub public (`justhman/*`).  
> Weights: S3 via entrypoint (`WEIGHTS_S3_URI`). Secrets: SSM SecureString.

## Port / health / image / arch

| service | port | health | hub image | arch |
|---|---|---|---|---|
| backend | 8800 | `/api/v1/health/live`, `/api/v1/health/ready` | `justhman/ai-live-backend` | arm64 |
| llm | 8001 | `/health` | `justhman/ai-live-llm` | amd64+gpu |
| tts | 8002 | `/health` | `justhman/ai-live-tts` | amd64+gpu |
| avatar | 8080 | `/health` | `justhman/ai-live-avatar` | amd64+gpu |
| livekit | 7880 + UDP 50000-60000 | `/` or livekit health | `justhman/ai-live-livekit` | arm64 |
| lmcache | 5555 zmq + 8080 metrics | `:8080/metrics` | `justhman/ai-live-lmcache` | arm64 |

## ECS task note

- **LLM + TTS** = 2 containers / 1 ECS Task / 1 GPU (`g6.xlarge`).
- Only the LLM container declares the GPU resource; TTS shares via `NVIDIA_VISIBLE_DEVICES`.
- GPU memory utilization: **LLM 0.6 / TTS 0.25** (~0.15 buffer).
- Backend + LiveKit = Fargate Spot ARM. Avatar = separate GPU task (`g4dn`). LMCache = EC2 Spot ARM (desired_count=0 when disabled).

## Minimum environment variables

### backend

```
APP_ENV                  # dev | staging | prod
LLM_BASE_URL             # e.g. http://llm:8001
TTS_BASE_URL             # e.g. http://tts:8002
AVATAR_BASE_URL          # e.g. http://avatar:8080
LIVEKIT_URL              # e.g. ws://livekit:7880
REDIS_URL                # redis://...
DATABASE_URL             # postgresql://...
LMCACHE_ENABLED          # true | false
# + SSM-injected secrets (API tokens, DB password, LiveAvatar key, etc.)
```

### llm

```
MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit
ENABLE_PREFIX_CACHING=1
GPU_MEMORY_UTILIZATION=0.6
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/...
# when LMCACHE_ENABLED=true:
#   PYTHONHASHSEED=0
#   LMCACHE_CONFIG_FILE=/app/lmcache_config.yaml
#   vLLM --kv-transfer-config LMCacheMPConnector
```

### tts

```
MODEL_ID=pnnbao-ump/VieNeu-TTS-v2
GPU_MEMORY_UTILIZATION=0.25
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/...
```

### avatar / livekit / lmcache

```
# avatar
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/...
# livekit — LIVEKIT_* keys via SSM
# lmcache — only scheduled when LMCACHE_ENABLED=true
```

## Out of contract (do not reintroduce)

- NAT gateway / private subnet modules for MVP
- ECR (use Docker Hub public)
- Secrets Manager (use SSM SecureString)
- Route53 / AWS WAF modules
- torch/vLLM pins in root backend packaging (GPU images own those)
