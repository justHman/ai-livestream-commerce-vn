# Live smoke (real AWS) + mandatory teardown

> Goal: prove the stack runs for real, keep logs, then **delete billable resources** so money stops.  
> Account: `191918535424` · Region: `ap-northeast-2` · Images: `imjusthman/*` · GitHub: `justHman/ai-livestream-commerce-vn`  
> SNS email (primary): `hmantools11@gmail.com`

## Cloudflare — first live smoke?

**Not required.**

| Path | Need Cloudflare? | How you hit the API |
|---|---|---|
| **First smoke (recommended)** | **No** | `http://<ALB-DNS>/api/v1/...` |
| Later prod-like | Yes (Free) | DNS CNAME → ALB + Full (strict) SSL |

Do **not** block bootstrap waiting for Cloudflare. Add CF after ALB health is green.

When you *do* want CF later (optional, 15 min):

1. Sign up https://dash.cloudflare.com (free)  
2. Add site → change nameservers at domain registrar  
3. DNS → CNAME `api` → ALB DNS name (proxy orange optional for HTTP first)  
4. SSL/TLS → **Full** (not Full strict until ACM cert on ALB)  
5. One rate-limit rule optional  

No Cloudflare API token needed for first smoke.

---

## What “real enough” means (chosen tier)

You asked for: **everything that can run for real and correctly**, with logs, then **tear down expensive stuff**.

### Tier S — Session smoke (default first day)

| On | Off |
|---|---|
| VPC, 2 public AZ, S3 GW EP, IGW | NAT / ECR / WAF / Route53 |
| ALB | GPU ASG desired > 0 |
| RDS t4g.medium + Redis t4g.small | LMCache |
| ECS Fargate **backend** only | g6 / g4dn |
| CloudWatch logs | Long-lived Spot GPU |
| SNS → `hmantools11@gmail.com` | — |

Backend env (task definition): `RENDER_BACKEND=mock`, `LLM_ENGINE=none`, `TTS_ENGINE=tone`, `SESSION_STORE=memory` (smoke path; Redis/RDS still provisioned but not required for mock health).

**Proves:** Terraform, ALB, RDS, Redis, ECS, images, health, lite session API, logs.  
**Does not prove:** vLLM/Omni GPU, real LiveKit media, avatar model, Redis session ownership.

### Lessons from 2026-07-13 smoke (PASS then destroy)

| Symptom | Root cause | Fix now in code |
|---------|------------|-----------------|
| `ModuleNotFoundError: numpy` | image deps incomplete | `pyproject` + uv multi-stage Dockerfile; include `providers/` |
| Target unhealthy | ALB health path `/health` | default `health_check_path=/api/v1/health/live` |
| ALB timeout, target healthy | SG only 443; listener :80 | open SG :80 when `certificate_arn=""` |
| GPU service invalid FARGATE | EC2 task + FARGATE_SPOT fallback | `count = create_ec2_capacity` on GPU services |
| SG create invalid | Unicode in description | ASCII-only SG descriptions |

Auth map: `/health*` open; `/lite/*` = viewer token; `/engines*` = admin token.  
Smoke log dir: `.runtime/smoke-20260713/`. CF CNAME `api.livento.me` created (DNS only); update content after every ALB recreate.

### Tier G — GPU burst (optional same day, **time-boxed**)

After Tier S green, for **≤ 2 hours**:

| On (temporary) | Still off |
|---|---|
| `desired_llm_tts=1` (g6 Spot) | LMCache |
| `desired_avatar=1` (g4dn Spot) optional | Multi-AZ RDS |
| LiveKit Fargate `desired_livekit=1` optional | — |

Then **immediately** set desired back to 0 **or** destroy stack (see teardown).

### Cost intuition (Seoul, rough)

| Tier | While running | After full teardown |
|---|---|---|
| S (API-only) | ~$3–6 / day | **$0** if destroy complete |
| G (GPU on) | tens of USD / **hour** class risk | **$0** only if ASG/ECS desired=0 **and** instances terminated |
| Forgotten GPU Spot 24h | **very expensive** | — |

**Iron rule:** no “leave it overnight” unless you explicitly accept the bill.

---

## Pre-flight (you)

- [x] AWS CLI on account `191918535424`, region `ap-northeast-2` (root OK for this bootstrap)  
- [ ] `docker login -u imjusthman`  
- [ ] GitHub secrets: `DOCKERHUB_USER=imjusthman`, `DOCKERHUB_TOKEN=<PAT>`  
- [ ] `AWS_ROLE_ARN_DEV` after OIDC role exists (or use local AWS CLI for first apply)  
- [ ] Email: billing + SNS → **`hmantools11@gmail.com`**  
- [ ] Confirm wall-clock: smoke window max **3 hours** then teardown  

---

## Step-by-step — Tier S

### 1) State backend (once)

```powershell
$ACCOUNT = "191918535424"
$REGION  = "ap-northeast-2"
$BUCKET  = "ai-livestream-tfstate-$ACCOUNT"

aws s3api create-bucket --bucket $BUCKET --region $REGION `
  --create-bucket-configuration LocationConstraint=$REGION
aws s3api put-bucket-versioning --bucket $BUCKET `
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket $BUCKET `
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws dynamodb create-table --table-name ai-livestream-tf-lock `
  --attribute-definitions AttributeName=LockID,AttributeType=S `
  --key-schema AttributeName=LockID,KeyType=HASH `
  --billing-mode PAY_PER_REQUEST --region $REGION
```

Uncomment S3 backend in `infra/environments/global/backend.tf` and `dev/backend.tf` with this bucket/table.

### 2) Global OIDC (once)

```powershell
cd infra\environments\global
cp terraform.tfvars.example terraform.tfvars
# set github org/user = justHman, repo = ai-livestream-commerce-vn
# alert_email = hmantools11@gmail.com if present
terraform init
terraform plan -var-file=terraform.tfvars -out=global.tfplan
terraform apply global.tfplan
```

Save deploy role ARN → GitHub `AWS_ROLE_ARN_DEV` when ready.

### 3) Dev stack (API-only)

`terraform.tfvars` (dev):

```hcl
env         = "dev"
project     = "ai-livestream"
aws_region  = "ap-northeast-2"
alert_email = "hmantools11@gmail.com"

desired_backend     = 1
desired_llm_tts     = 0
desired_avatar      = 0
desired_livekit     = 0
desired_lmcache     = 0
lmcache_enabled     = false
create_ec2_capacity = false   # no GPU ASG until Tier G

image_backend = "imimjusthman/ai-live-backend:dev"
```

```powershell
cd ..\dev
$env:TF_VAR_db_password = "<strong password not in git>"
terraform init
terraform plan -var-file=terraform.tfvars -out=dev.tfplan
# READ PLAN: no NAT, no ECR, no WAF, no g6/g4dn if create_ec2_capacity=false
terraform apply dev.tfplan
```

Capture outputs: `alb_dns`, `rds_endpoint`, `redis_endpoint`, `ecs_cluster`, `s3_bucket`.

### 4) Image + task env

```powershell
cd <implementations root>
docker login -u imjusthman
# ARM64 for Fargate Spot (Graviton)
docker build --platform linux/arm64 -f services/backend/Dockerfile -t imimjusthman/ai-live-backend:dev .
# Quick import check (mock path)
docker run --rm --platform linux/arm64 `
  -e RENDER_BACKEND=mock -e LLM_ENGINE=none -e TTS_ENGINE=tone `
  -e SESSION_STORE=memory -e APP_ENV=dev `
  --entrypoint python imimjusthman/ai-live-backend:dev `
  -c "from core.server import app; print(type(app).__name__)"
docker push imimjusthman/ai-live-backend:dev
# Dockerfile: multi-stage uv → /opt/venv, non-root appuser, copies core/ + providers/
```

Task definition injects mock env + tokens via TF vars (`TF_VAR_backend_api_token`, `TF_VAR_admin_api_token`). ALB health path is `/api/v1/health/live`.

Smoke sketch:

```powershell
$base = "http://$(terraform -chdir=infra/environments/dev output -raw alb_dns_name)"
$H_v = @{ Authorization = "Bearer $env:TF_VAR_backend_api_token"; "Content-Type"="application/json" }
$H_a = @{ Authorization = "Bearer $env:TF_VAR_admin_api_token";  "Content-Type"="application/json" }
Invoke-WebRequest "$base/api/v1/health/live" -UseBasicParsing
Invoke-WebRequest "$base/api/v1/engines" -Headers $H_a -UseBasicParsing
$sid = ((Invoke-WebRequest "$base/api/v1/lite/start" -Method POST -Headers $H_v -Body '{}' -UseBasicParsing).Content | ConvertFrom-Json).session_id
Invoke-WebRequest "$base/api/v1/lite/say" -Method POST -Headers $H_v -Body (@{session_id=$sid;text="smoke"}|ConvertTo-Json) -UseBasicParsing
Invoke-WebRequest "$base/api/v1/lite/stop" -Method POST -Headers $H_v -Body (@{session_id=$sid}|ConvertTo-Json) -UseBasicParsing
# then: terraform -chdir=infra/environments/dev destroy -auto-approve -var-file=terraform.tfvars
# if Redis already gone mid-destroy: terraform state rm module.database.aws_elasticache_cluster.redis; destroy -refresh=false
```

Backend task env (minimum — wired by Terraform compute module):

```
APP_ENV=dev
RENDER_BACKEND=mock
LLM_ENGINE=none
TTS_ENGINE=tone
SESSION_STORE=memory
BACKEND_API_TOKEN=<TF_VAR_backend_api_token>
ADMIN_API_TOKEN=<TF_VAR_admin_api_token>
DEBUG_ENABLED=true
DIRECTOR_ENABLED=1
LMCACHE_ENABLED=false
PIPECAT_ENABLED=0
LIVEKIT_PUBLISH=0
LMCACHE_ENABLED=false
PIPECAT_ENABLED=false
LIVEKIT_PUBLISH=false
```

Apply DB schema:

```powershell
psql $env:DATABASE_URL -f core\sql\runtime_schema.sql
```

### 5) Smoke + **log capture** (mandatory)

```powershell
$BASE = "http://<ALB_DNS>"
$LOGDIR = "D:\Project\Code\research\projects\ai-livestream-commerce-vn\implementations\.runtime\smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null

curl.exe -fsS "$BASE/api/v1/health/live"  | Tee-Object "$LOGDIR\01-live.json"
curl.exe -fsS "$BASE/api/v1/health/ready" | Tee-Object "$LOGDIR\02-ready.json"

curl.exe -fsS -X POST "$BASE/api/v1/sessions" `
  -H "Authorization: Bearer $env:BACKEND_API_TOKEN" `
  -H "Content-Type: application/json" -d "{}" `
  | Tee-Object "$LOGDIR\03-session-start.json"

# ECS logs (replace cluster/service)
aws logs tail /ecs/ai-livestream-dev/backend --since 30m --format short `
  | Tee-Object "$LOGDIR\04-ecs-backend.log"

# Cost snapshot
aws ce get-cost-and-usage --time-period Start=$(Get-Date -Format yyyy-MM-01),End=$(Get-Date -Format yyyy-MM-dd) `
  --granularity MONTHLY --metrics UnblendedCost `
  | Tee-Object "$LOGDIR\05-cost-month.json"

# Write PASS/FAIL summary
@"
smoke_at: $(Get-Date -Format o)
base: $BASE
tier: S
health_live: see 01-live.json
session: see 03-session-start.json
teardown_required: YES
"@ | Set-Content "$LOGDIR\SUMMARY.md"
```

**PASS criteria Tier S**

- [ ] `/health/live` 200  
- [ ] `/health/ready` 200 (or ready=false only if documented missing GPU engines)  
- [ ] `POST /sessions` returns session id  
- [ ] ECS log stream has request lines  
- [ ] `SUMMARY.md` written under `.runtime/smoke-*`  

---

## Teardown (non-negotiable after smoke)

### Fast path — stop money, keep code/state optional

```powershell
# 1) Scale everything to zero
aws ecs update-service --cluster <cluster> --service <backend-service> --desired-count 0
# if GPU was on:
# aws autoscaling set-desired-capacity --auto-scaling-group-name <g6-asg> --desired-capacity 0
# aws autoscaling set-desired-capacity --auto-scaling-group-name <g4dn-asg> --desired-capacity 0

# 2) Confirm no running expensive instances
aws ec2 describe-instances --filters Name=instance-state-name,Values=running `
  --query "Reservations[].Instances[].[InstanceId,InstanceType,State.Name]" --output table
```

### Full destroy — **recommended after first successful smoke**

```powershell
cd infra\environments\dev
terraform destroy -var-file=terraform.tfvars
# type yes

# Verify empty
aws ecs list-clusters
aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"
aws elasticache describe-cache-clusters --query "CacheClusters[].CacheClusterId"
aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName"
aws ec2 describe-nat-gateways --filter Name=state,Values=available  # must be empty
```

**Keep** (cheap): S3 tfstate bucket, DynamoDB lock, OIDC provider (global).  
**Delete if you want zero residual:** empty S3 weights bucket versions, then destroy global too.

### Post-teardown log

Append to `$LOGDIR\SUMMARY.md`:

```
teardown_at: <iso>
terraform_destroy: yes|no
running_ec2_after: 0
nat_gateways: 0
```

---

## Tier G (only if Tier S PASS and you accept GPU bill)

1. `create_ec2_capacity = true`  
2. `desired_llm_tts = 1` (and avatar/livekit if needed)  
3. Push GPU images + `WEIGHTS_S3_URI`  
4. Smoke engines `/health/ready` + one `/sessions/{id}/say`  
5. **Within 2h:** desired=0 **or** `terraform destroy`  
6. Same log folder + cost check  

---

## SNS / billing emails

| Email | Role |
|---|---|
| **hmantools11@gmail.com** | Primary SNS + billing alerts |
| hmanclubs11@gmail.com | Optional second SNS subscription later |
| hoangdvse183095@fpt.edu.vn | Optional |

Terraform: `alert_email = "hmantools11@gmail.com"`.  
Billing console: add same email; confirm subscription mail.

---

## Do not do on first smoke

- Wait for Cloudflare  
- Leave GPU desired=1 overnight  
- Enable NAT “just in case”  
- Push to prod tag `v*`  
- Use Secrets Manager / ECR  

---

## Done definition (this runbook)

- [ ] Tier S green with logs under `.runtime/smoke-*`  
- [ ] Teardown complete (scale-to-zero **or** destroy)  
- [ ] No running g6/g4dn/NAT  
- [ ] Cloudflare still optional / not blocking  
