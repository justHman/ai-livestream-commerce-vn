# Runbook: deployment preparation

> Offline preparation only. Do not apply Terraform, publish images, change DNS,
> create a release, or invoke a deployment workflow without separate approval.

## Preconditions

- AWS credentials that can bootstrap account-wide Terraform state and OIDC.
- Docker Hub credentials for the `imjusthman` namespace.
- GitHub secrets: `AWS_ROLE_ARN_DEV`, `AWS_ROLE_ARN_PROD`, `DOCKERHUB_USER`, and
  `DOCKERHUB_TOKEN`.
- Runtime secrets supplied through SSM SecureString or `TF_VAR_*`, never a
  committed tfvars file.
- A bounded billable window and teardown decision for any Tier S execution.

## Runtime contract

| Concern | Tier S value | Later media/GPU value |
|---|---|---|
| Renderer | `mock` | approved cloud or self-host backend |
| LLM | `none` | remote/local engine supported by image contract |
| TTS | `tone` | remote/local engine supported by image contract |
| Session metadata | `memory` | `redis` after its deployment is tested |
| Runtime Postgres | omitted unless SSM DSN ARN configured | `DATABASE_URL` from SSM |
| LiveKit publisher | `0` | `1` only for an approved real-SFU test |
| GPU/media services | desired count `0` | separately enabled and cost-bounded |

The backend image reads `BACKEND_API_TOKEN`, `ADMIN_API_TOKEN`, and optionally
`DATABASE_URL` from SSM. The Terraform module deliberately sets
`LIVEKIT_PUBLISH=0`; enabling it requires a new task-definition contract and a
real media verification.

## Offline gate

```powershell
uv lock --check
uv run pytest core/tests/ -q
uvx ruff check core/api core/db core/debug core/director core/llm core/render core/schemas core/stream core/tts core/config.py core/engine_manager.py core/livekit_publish.py core/livekit_tokens.py core/pipecat_bridge.py core/server.py core/store.py providers scripts/bench_api.py scripts/upload_weights_s3.py
terraform fmt -check -recursive infra
terraform -chdir=infra/environments/global init -backend=false
terraform -chdir=infra/environments/global validate
terraform -chdir=infra/environments/dev init -backend=false
terraform -chdir=infra/environments/dev validate
terraform -chdir=infra/environments/prod init -backend=false
terraform -chdir=infra/environments/prod validate
```

## Image contract

| Image | Dockerfile | Platform |
|---|---|---|
| `imjusthman/ai-live-backend` | `services/backend/Dockerfile` | `linux/arm64` |
| `imjusthman/ai-live-llm` | `services/llm/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-tts` | `services/tts/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-avatar` | `services/avatar/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-livekit` | `services/livekit/Dockerfile` | `linux/arm64` |
| `imjusthman/ai-live-lmcache` | `services/lmcache/Dockerfile` | `linux/arm64` |

DEV builds only backend when deployed Tier S outputs have zero effective optional
service counts. PROD tag push builds six immutable images but does not deploy.

## Bootstrap

1. Copy `infra/environments/global/terraform.tfvars.example` to ignored
   `terraform.tfvars`; set the two bootstrap booleans for the one local-state
   bootstrap.
2. Review the global plan and apply only after approval.
3. Migrate global state to S3 after the bucket exists.
4. Copy `dev/terraform.tier-s.tfvars.example` to ignored `terraform.tfvars`.
   Supply passwords and tokens through `TF_VAR_*` environment variables.
5. Review the DEV plan: backend=1, optional desired counts=0,
   `create_ec2_capacity=false`, mock/none/tone/memory.

Use [runbook-live-smoke-and-teardown.md](./runbook-live-smoke-and-teardown.md)
for approved execution and teardown.

## Smoke origin

After apply, derive the origin from Terraform rather than a remembered hostname:

```powershell
$scheme = terraform -chdir=infra/environments/dev output -raw alb_url_scheme
$host = terraform -chdir=infra/environments/dev output -raw alb_dns_name
$base = "$scheme://$host"
```

Cloudflare is optional after ALB health is known.