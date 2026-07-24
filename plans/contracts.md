# Service contracts

> Current deployment contract. It names Terraform/workflow artifacts; it is not
> evidence that a service is deployed.

## Ports, health, images, platforms

| Service | Port / health | Image | Platform | Tier S |
|---|---|---|---|---|
| backend | `8800`, `/api/v1/health/live`, `/api/v1/health/ready` | `imjusthman/ai-live-backend` | arm64 | enabled |
| llm | `8001`, `/health` | `imjusthman/ai-live-llm` | amd64 GPU | disabled |
| tts | `8002`, `/health` | `imjusthman/ai-live-tts` | amd64 GPU | disabled |
| avatar | `8080`, `/health` | `imjusthman/ai-live-avatar` | amd64 GPU | disabled |
| livekit | `7880`, UDP media | `imjusthman/ai-live-livekit` | arm64 | disabled |
| lmcache | `5555`, metrics `8080` | `imjusthman/ai-live-lmcache` | arm64 | disabled |

LLM/TTS share one EC2 GPU task. LLM requests the ECS GPU resource; TTS shares
it process-side. Backend/LiveKit use Fargate Spot ARM. Avatar/LMCache exist
only when `create_ec2_capacity=true`.

## Backend environment

```text
APP_ENV=dev|prod
RENDER_BACKEND=mock|cloud_liveavatar|remote_avatar|self_host_*
LLM_ENGINE=none|openai_compat|vllm|sglang|hf|llamacpp
LLM_BASE_URL=
TTS_ENGINE=tone|remote_http|transformers|vieneu|cosyvoice
TTS_BASE_URL=
SESSION_STORE=memory|redis
REDIS_URL=
DATABASE_URL=                         # configured SSM ARN only
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
LIVEKIT_PUBLISH=0                     # fixed in current Tier S task definition
PIPECAT_ENABLED=0
LMCACHE_ENABLED=false
BACKEND_API_TOKEN=                    # SSM
ADMIN_API_TOKEN=                      # SSM
```

`DATABASE_URL` is durable runtime persistence, not `SESSION_STORE`.
`LIVEKIT_PUBLISH=1` is supported by backend code but outside default Terraform
Tier S.

## Tier S values

```text
backend=1; llm_tts=0; avatar=0; livekit=0; lmcache=0
create_ec2_capacity=false
mock + none + tone + memory
```

The profile is `infra/environments/dev/terraform.tier-s.tfvars.example`. Copy
it to ignored `terraform.tfvars` only for an explicitly approved run.

## MVP exclusions

No NAT, private subnet, ECR, Secrets Manager, Route53, AWS WAF, weights in
images, `/user/*`, or `/shop/*`.
