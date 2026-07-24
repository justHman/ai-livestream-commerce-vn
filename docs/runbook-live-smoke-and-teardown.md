# Tier S live smoke and teardown

> **Billable external operation.** Execute only after explicit confirmation of
> the AWS account, resources, time window, estimated cost, smoke commands, and
> teardown choice. This procedure is not authorization.

## Scope

Tier S enables one mock API backend and keeps all media/GPU compute inactive.

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
3. Copy the Tier S profile to ignored DEV tfvars; supply the database password
   and both API tokens through `TF_VAR_*` outside shell history.

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

## Next live phases

Real LiveKit audio/browser smoke, avatar video publishing, GPU services, and
PROD deployment each require a separate approval with their own cost and
rollback plan.
