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

## Three-stage deploy ladder

Three progressive live stages, increasing fidelity and cost. Stage N MUST NOT
start until Stage N-1 records PASS and teardown verification. Iron rule:
**my money is your money** — teardown before fix, teardown after success.
See [runbook-live-smoke-and-teardown.md](./runbook-live-smoke-and-teardown.md)
for the mandatory loop and the money-safe boot procedure.

| Stage | Profile (committed example) | Engines | Capacity | LiveKit |
|---|---|---|---|---|
| 1 Mock | `terraform.tier-s.tfvars.example` | `mock` / `none` / `tone` | backend=1, GPU/optional=0 | off |
| 2 LiveAvatar cloud | `terraform.stage-2-liveavatar.tfvars.example` (to create) | `cloud_liveavatar` + real LLM (vLLM Qwen3.5-4B-AWQ) + real TTS (provider runtime, VieNeu v3 Turbo) | backend=1; `desired_llm/desired_tts=1` Spot g6.xlarge (L4 24GB); avatar=0 | **off** (`desired_livekit=0`) |
| 3 Self-host avatar | `terraform.stage-3-selfhost.tfvars.example` (to create) | `self_host_avatarforcing_half` + same LLM/TTS as Stage 2 | backend=1; `desired_llm/desired_tts=1` Spot g6; `desired_avatar=1` Spot g4dn.xlarge; `create_ec2_capacity=true` | **on** (`desired_livekit=1`, `LIVEKIT_PUBLISH=1`) |

Stage 2 avatar is LiveAvatar cloud → video flows cloud → browser directly, no
SFU. Stage 3 is the first stage that needs LiveKit (self-host avatar publishes
through the SFU). The LLM/TTS engine path is identical Stage 2→3 so the only
variable is the avatar backend (true ablation).

### Iron rules (apply to every stage)

1. **Teardown-first**: FAIL → destroy+verify → offline fix → re-deploy; PASS → report → destroy+verify. No idle billable infra while coding/debugging/waiting.
2. **No backup pile-up**: DEV RDS `backup_retention_days=0`, `skip_final_snapshot=true`, no manual snapshots; DEV S3 `enable_versioning=false`, `force_destroy=true`; PROD S3 `lifecycle_noncurrent_days=1`.
3. **Money-safe boot**: first apply of any stage sets all cost-driving desired/env OFF (`0`/`false`/`none`); setup (GitHub Actions SHA images, S3 weights seed, SSM secrets, config validate) completes offline; only then a second apply scales the cost-driving desired counts on.

### Spot + ARM + image contract (default)

| Surface | Default | Notes |
|---|---|---|
| Backend / LiveKit (Fargate) | `FARGATE_SPOT` + `arm64` | image platform `linux/arm64` |
| LLM+TTS engine box (Stage 2/3) | Spot `g6.xlarge` (L4 24GB), x86 | one box, two processes, GPU partitioned 0.55/0.35/0.10 |
| Avatar GPU (Stage 3) | Spot `g4dn.xlarge` (T4), x86 | no ARM GPU tier at this size |
| LMCache | **disabled** Stage 2/3 | only for 2+ replicas cross-replica KV share |
| Images | GitHub Actions, SHA-tagged, arch-matched | no `:latest`, no hand-push on billable stages |

Spot quota `ap-northeast-2` (verified 2026-07-24): G/VT Spot 8 vCPU approved.
Stage 3 peak = g6 engine 4 + g4dn avatar 4 = 8 vCPU = quota ceiling
(single-avatar smoke only). GPU AZs: `g6.xlarge` 2a/2c/2d, `g4dn.xlarge`
2a/2b/2c. No new quota request needed for the planned smoke.

### LiveAvatar key (Stage 2)

`LIVEAVATAR_API_KEY` is a backend-only secret. Stage 2 injects it from an SSM
SecureString parameter into the backend task definition through the compute
module `secrets_arns` map (compute `main.tf` injects `LIVEAVATAR_API_KEY` when
`secrets_arns` contains the `liveavatar/api_key` key — absent in Stage 1/3, so
no effect on mock/self-host stages).

Wire steps (out-of-band, never committed):

1. Create the SSM SecureString parameter, e.g.
   `aws ssm put-parameter --name /ai-livestream/dev/liveavatar/api_key --type SecureString --value <key> --overwrite`.
2. Ensure the secrets module exposes its ARN under the
   `liveavatar/api_key` key in `parameter_arns` (extend `infra/modules/secrets`
   if it does not already manage this parameter). If the secrets module is not
   extended, pass the ARN into `secrets_arns` via a dedicated root variable.
3. The compute backend task definition then injects it as `LIVEAVATAR_API_KEY`.

The key MUST NOT appear in tfvars, logs, or stage reports. First smoke uses
the sandbox avatar `LIVEAVATAR_SANDBOX_AVATAR_ID` (default
`dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`, free ~1-min, no credits); switch to a
real credit-charged avatar only for the formal benchmark.

## Runtime contract

| Concern | Tier S value | Later media/GPU value |
|---|---|---|
| Renderer | `mock` | `cloud_liveavatar` (Stage 2) / `self_host_avatarforcing_half` (Stage 3) |
| LLM | `none` | `vllm` serving `cyankiwi/Qwen3.5-4B-AWQ-4bit` (Stage 2/3) |
| TTS | `tone` | provider runtime — VieNeu v3 Turbo default (`TTS_PROVIDER=vieneu_v3`) (Stage 2/3) |
| Session metadata | `memory` | `redis` after its deployment is tested |
| Runtime Postgres | omitted unless SSM DSN ARN configured | `DATABASE_URL` from SSM |
| LiveKit publisher | `0` | `1` only in Stage 3 (self-host avatar through SFU) |
| GPU/media services | desired count `0` | `desired_llm/desired_tts=1` Stage 2/3, `desired_avatar=1` Stage 3 |

The backend image reads `BACKEND_API_TOKEN`, `ADMIN_API_TOKEN`, and optionally
`DATABASE_URL` from SSM. The Terraform module deliberately sets
`LIVEKIT_PUBLISH=0`; enabling it requires a new task-definition contract and a
real media verification.

## Offline gate

```powershell
uv lock --check
uv run pytest tests/ci/ -q
uvx ruff check services/product/backend_service/src services/product/llm_service/src services/product/tts_service/src services/product/avatar_service/src services/product/*_service/scripts benchmarks/api/latency.py scripts/model_assets/upload.py
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
| `imjusthman/ai-live-backend` | `services/product/backend_service/Dockerfile` | `linux/arm64` |
| `imjusthman/ai-live-llm` | `services/product/llm_service/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-tts` | `services/product/tts_service/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-avatar` | `services/product/avatar_service/Dockerfile` | `linux/amd64` |
| `imjusthman/ai-live-livekit` | `services/platform/livekit/Dockerfile` | `linux/arm64` |
| `imjusthman/ai-live-lmcache` | `services/platform/lmcache/Dockerfile` | `linux/arm64` |

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