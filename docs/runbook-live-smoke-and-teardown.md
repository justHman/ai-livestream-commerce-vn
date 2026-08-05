# Tier S live smoke and teardown

> **Billable external operation.** Execute only after explicit confirmation of
> the AWS account, resources, time window, estimated cost, smoke commands, and
> teardown choice. This procedure is not authorization.
>
> **Iron rule — my money is your money.** Billable DEV infrastructure is
> money-burning while it runs. Teardown is mandatory before any debug/fix work
> and after every stage success/report. No idle window while coding, debugging,
> or waiting. No multi-day "stopped" holding pattern. See the three-stage ladder
> and money-safe boot in [runbook-deploy-prep.md](./runbook-deploy-prep.md).

## Mandatory loop (every stage)

```text
offline gate → plan → human approve → apply → smoke/bench →
  FAIL: report → destroy+verify → offline fix → (loop back to plan)
  PASS: report → destroy+verify → promote to next stage OR stop
```

- Live apply and destroy MUST NOT use `-auto-approve` as the default path.
- Re-test/re-benchmark after a failure runs only on a freshly deployed stack
  after the offline fix; never against a stack left up "to save time".
- Temporary `desired-count=0` stop is second-class: list remaining billables
  (ALB, RDS, Redis), same-day only, full destroy before leaving the workday
  unless the user explicitly extends the window.

## Money-safe boot (every stage)

Two-phase apply per stage. Phase 0 = zero-cost apply (all cost-driving
desired/env OFF) → verify stack healthy → setup offline (GitHub Actions SHA
images pushed, S3 weights seeded, SSM secrets present, config validated) →
Phase 1 = scale-up apply (cost-driving desired on). The gap between Phase 0
and Phase 1 MUST NOT pay for an idle box waiting on setup.

```text
Phase 0: apply with desired_llm/desired_tts/avatar/livekit/lmcache=0, create_ec2_capacity per stage, engines off/mock
  → verify healthy at zero compute cost
  → offline: build+push SHA images (GitHub Actions), seed weights to S3, put SSM secrets, validate config
Phase 1: apply again with the stage's cost-driving desired counts ON
  → smoke/bench
  → FAIL: scale back to 0 (or full destroy) → offline fix → repeat Phase 0→1
  → PASS: report → destroy+verify
```

## Scope

Tier S = Stage 1 of the three-stage ladder. It enables one mock API backend
and keeps all media/GPU compute inactive. Stage 2 (LiveAvatar cloud + real
engines, no LiveKit) and Stage 3 (self-host avatar + LiveKit) follow the same
mandatory loop and money-safe boot, with their own tfvars profiles and smoke
commands documented under each stage below.

| Enabled | Disabled |
|---|---|
| VPC, public subnets, IGW, S3 gateway endpoint | NAT, ECR, WAF, Route53 |
| ALB and one backend Fargate task | EC2 capacity and GPUs |
| RDS and Redis infrastructure | LLM/TTS, avatar, LiveKit, LMCache services |
| CloudWatch/SNS when enabled | `LIVEKIT_PUBLISH` |

Runtime values: `RENDER_BACKEND=mock`, `LLM_ENGINE=none`, `TTS_ENGINE=tone`,
`SESSION_STORE=memory`, and `APP_ENV=dev`. RDS/Redis may exist but are unused
by the default runtime profile. `DATABASE_URL` is absent unless an SSM parameter
ARN was configured.

Tier S proves Terraform, ALB, backend image, auth, session APIs, logs, and
teardown. It cannot prove LiveKit, browser media, remote engines, Redis HA,
Postgres persistence, avatar models, or GPUs.

## Before apply

1. Run the full offline gate in `SHIP-CHECKLIST-DEPLOY-PREP.md`.
2. Bootstrap global remote state per `terraform-layout.md` if absent.
3. Copy the Stage 1 (Tier S) profile to ignored DEV tfvars; supply the database
   password and both API tokens through `TF_VAR_*` outside shell history.

```powershell
Copy-Item infra/environments/dev/terraform.tier-s.tfvars.example infra/environments/dev/terraform.tfvars
terraform -chdir=infra/environments/dev init
terraform -chdir=infra/environments/dev plan -var-file=terraform.tfvars -out=tier-s.tfplan
```

Confirm no EC2 capacity and no positive optional desired count. Do not use
`-auto-approve`; this is the live-operation boundary:

```powershell
# Requires explicit approval immediately before execution.
# terraform -chdir=infra/environments/dev apply tier-s.tfplan
```

## Discover origin and capture logs

```powershell
$scheme = terraform -chdir=infra/environments/dev output -raw alb_url_scheme
$host = terraform -chdir=infra/environments/dev output -raw alb_dns_name
$base = "$scheme://$host"
$logDir = Join-Path ".runtime" ("tier-s-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
```

Do not use a remembered domain or Cloudflare hostname for `$base`.

## Smoke

```powershell
$viewer = @{ Authorization = "Bearer $env:TF_VAR_backend_api_token"; "Content-Type" = "application/json" }

curl.exe -fsS "$base/api/v1/health/live" | Tee-Object "$logDir/01-live.json"
curl.exe -fsS "$base/api/v1/health/ready" | Tee-Object "$logDir/02-ready.json"
curl.exe -fsS "$base/api/v1/engines" -H "Authorization: Bearer $env:TF_VAR_admin_api_token" | Tee-Object "$logDir/03-engines.json"

$started = Invoke-RestMethod "$base/api/v1/sessions" -Method Post -Headers $viewer -Body "{}"
$started | ConvertTo-Json | Tee-Object "$logDir/04-session-start.json"
$sid = $started.session_id

Invoke-RestMethod "$base/api/v1/sessions/$sid/attach" -Method Post -Headers $viewer -Body '{"products":[]}' | ConvertTo-Json | Tee-Object "$logDir/05-attach.json"
Invoke-RestMethod "$base/api/v1/sessions/$sid/plan/create" -Method Post -Headers $viewer -Body '{"products":[]}' | ConvertTo-Json | Tee-Object "$logDir/06-plan.json"
Invoke-RestMethod "$base/api/v1/sessions/$sid/chat" -Method Post -Headers $viewer -Body '{"text":"smoke","author":"tier-s"}' | ConvertTo-Json | Tee-Object "$logDir/07-chat.json"
Invoke-RestMethod "$base/api/v1/sessions/$sid/stop" -Method Post -Headers $viewer -Body "{}" | ConvertTo-Json | Tee-Object "$logDir/08-session-stop.json"
```

Capture CloudWatch backend logs and write timestamp/result to
`$logDir/SUMMARY.md`, excluding secrets and Authorization values.

Pass requires liveness, readiness, authenticated engine access, complete
session lifecycle, and captured backend request logs.

## Teardown

Choose and record one route before the smoke.

### Full destroy — preferred after a first smoke

```powershell
# Requires explicit approval immediately before execution.
# terraform -chdir=infra/environments/dev destroy -var-file=terraform.tfvars
```

### Temporary stop

```powershell
$cluster = terraform -chdir=infra/environments/dev output -raw ecs_cluster_name
$backend = terraform -chdir=infra/environments/dev output -json ecs_service_names | ConvertFrom-Json | Select-Object -ExpandProperty backend
# Requires explicit approval immediately before execution.
# aws ecs update-service --cluster $cluster --service $backend --desired-count 0
```

Temporary stop leaves ALB, RDS, Redis, and other resources billable. After
teardown record the action, time, remaining EC2 instances, and state decision.
A full destroy must verify ECS, RDS, ElastiCache, ALB, and unexpected NAT
resources are gone.

### Teardown verification — mandatory evidence

A destroy command without verification does NOT count as teardown complete.
Run `infra/scripts/teardown_verify.ps1` (or the checklist below) and write
`teardown-verify.md` into the stage log dir. Verification MUST also confirm
no backup/snapshot leftovers (iron rule: no backup pile-up).

```powershell
$env:AWS_REGION = "ap-northeast-2"
# ECS: zero RUNNING tasks across the cluster
$cluster = terraform -chdir=infra/environments/dev output -raw ecs_cluster_name
aws ecs list-tasks --cluster $cluster --desired-status RUNNING --query "taskArns" --output text
# RDS: instance gone, no manual/automated snapshots left
aws rds describe-db-instances --query "DBInstances[].Identifier" --output text
aws rds describe-db-snapshots --query "DBSnapshots[].DBSnapshotIdentifier" --output text
# ElastiCache: cluster gone
aws elasticache describe-cache-clusters --query "CacheClusters[].CacheClusterId" --output text
# ALB: no load balancers tagged for this env
aws elbv2 describe-load-balancers --query "LoadBalancers[?starts_with(LoadBalancerName,'ai-livestream-dev')].LoadBalancerName" --output text
# S3: no noncurrent versions on the assets bucket (DEV versioning should be off, so 0 expected)
$bucket = terraform -chdir=infra/environments/dev output -raw storage_bucket_id
aws s3api list-object-versions --bucket $bucket --query "Versions[].VersionId" --output text
aws s3api list-object-versions --bucket $bucket --query "DeleteMarkers[].VersionId" --output text
# EC2/NAT: no unexpected instances or NAT gateways
aws ec2 describe-instances --query "Reservations[].Instances[].InstanceId" --output text
aws ec2 describe-nat-gateways --query "NatGateways[].NatGatewayId" --output text
```

Any remaining billable resource, leftover RDS snapshot, or S3 noncurrent
version = teardown FAIL. Record each check's output (counts or empty) into
`teardown-verify.md`.

## Stage-exit report

Every stage attempt (PASS or FAIL) writes a `SUMMARY.md` into
`.runtime/stage-{1|2|3}-<timestamp>/` before promotion or session end. Use
the template in `docs/stage-exit-report-template.md`. Reports MUST exclude
secrets and Authorization values. FAIL reports MUST still include teardown
verification after destroy.

## Stage 2 — LiveAvatar cloud + real engines (no LiveKit)

> Billable: AWS g6 Spot + LiveAvatar credits. Requires Stage 1 PASS report +
> teardown verification before any Stage 2 plan.

Profile: `infra/environments/dev/terraform.stage-2-liveavatar.tfvars.example`
(`render_backend=cloud_liveavatar`, `llm_engine=vllm`, `tts_engine=vllm-omni`,
`desired_llm/desired_tts=1` Spot g6, `desired_livekit=0`, `desired_avatar=0`,
`desired_lmcache=0`/`LMCACHE_ENABLED=false`). Money-safe boot: Phase 0 with
`desired_llm/desired_tts=0`, then build+push SHA images, seed Qwen3.5-4B-AWQ +
VieNeu-TTS weights to S3 (local `.git` source for VieNeu — NOT public on HF),
put `LIVEAVATAR_API_KEY` in SSM, then Phase 1 with `desired_llm/desired_tts=1`.

Smoke (sandbox-first): use `LIVEAVATAR_SANDBOX_AVATAR_ID`
(`dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`, free ~1-min, no credits) for the
first smoke to prove LLM → TTS → LiveAvatar cloud avatar; switch to a real
credit-charged avatar only for the formal benchmark. Pass criteria: engines
endpoint reports real LLM (vLLM Qwen3.5-4B-AWQ) + real TTS (vllm-omni VieNeu);
one session completes the LLM → TTS → LiveAvatar cloud speak path; LiveAvatar
API key never in logs; `desired_livekit=0` verified; bounded latency sample
recorded. No LiveKit in Stage 2. On FAIL/PASS follow the mandatory loop.

## Stage 3 — self-host avatar + LiveKit full media

> Billable: AWS g6 Spot (engine) + g4dn Spot (avatar) + LiveKit Fargate Spot.
> Requires Stage 2 PASS report + teardown verification before any Stage 3
> plan. Stage 3 peak = 8 vCPU G/VT Spot = quota ceiling (single-avatar smoke).

Profile: `infra/environments/dev/terraform.stage-3-selfhost.tfvars.example`
(`render_backend=self_host_avatarforcing_half`, same LLM/TTS as Stage 2,
`desired_avatar=1`, `create_ec2_capacity=true`, Spot `g4dn.xlarge` avatar,
`desired_livekit=1`/`LIVEKIT_PUBLISH=1`, `desired_lmcache=0`). Money-safe boot:
Phase 0 with all cost-driving desired=0, then Phase 1 with
`desired_llm/desired_tts=1`, `desired_avatar=1`, `desired_livekit=1`.

Smoke: `self_host_avatarforcing_half` start/speak/avatar-video-publish-through-
LiveKit/stop or explicit fail-loud (no silent mock fallback). After bench
PASS run the FE localhost WebRTC check: bring up the workbench console pointing at the
Terraform-derived API origin and verify avatar video is visible in the
browser on `localhost` (API → LiveKit SFU → FE). This is a Stage 3 exit gate
before teardown. On FAIL/PASS follow the mandatory loop.

## Next live phases

Stage 2 / Stage 3 are documented above. PROD deployment requires a separate
approval with its own cost and rollback plan.
