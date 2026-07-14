# Plan 00 — Implement AWS stack (Terraform + Docker + CI/CD)

> Status: **ACTIVE — wait for master roadmap approval** (`02-master-implement-roadmap.md`).  
> Parent: `02-master-implement-roadmap.md` Phases 0–2, 6.  
> Confirmed: `docs/aws-architecture.md`, `docs/terraform-layout.md`, `docs/cicd-branch-strategy.md`, `docs/aws-pricing-seoul.csv`.

Do **not** re-open: NAT/private subnet, ECR MVP, Secrets Manager MVP, Route53/WAF, API Gateway, weights-in-image.

## Goal

Ship AWS Seoul path:

1. `infra/` Root & Child modules  
2. `services/*/Dockerfile` multi-stage + S3 weight entrypoint  
3. `.github/workflows/{ci,deploy-dev,deploy-prod}.yml`  
4. ECS task defs: Hub public images + SSM + S3 weights  

## As-is blockers this plan solves

| Gap | Today |
|---|---|
| G1 | no `infra/` |
| G2 | `services/*` empty dirs |
| G3 | no `.github/workflows` |
| G15 partial | no lmcache image/service |
| G16 | no root backend lockfile for image |

## Constraints

| Item | Value |
|---|---|
| Region | `ap-northeast-2` |
| Network | 2 public subnets across AZs, IGW, S3 Gateway Endpoint, no NAT |
| Edge | Cloudflare Free → ALB |
| Registry | Docker Hub public |
| Secrets | SSM SecureString |
| Weights | `s3://ai-livestream-{env}/weights/` entrypoint sync |
| MVP compute | Fargate Spot ARM (backend, livekit); EC2 Spot g6 (llm+tts Task), g4dn (avatar); c7g.2xlarge Spot LMCache |
| Data | RDS t4g.medium SA + 100GB gp3; Redis t4g.small |
| CI auth | OIDC only |

## Work packages (execution order)

### WP0 — Contracts (shared with Plan 01)

Freeze ports/env/image names from master roadmap §5. No AWS resources yet.

### WP1 — Terraform modules

| Module | Contents |
|---|---|
| `network` | VPC, 2 public subnets across AZs, IGW, routes, S3 GW EP |
| `security` | SG matrix aws-architecture §3; OIDC; task/exec/deploy roles; IMDSv2; no :22 |
| `compute` | ECS cluster; CP Fargate Spot + EC2 Spot GPU ASG (g6/g4dn); LMCache c7g ASG; 4 task defs / 4 services |
| `database` | RDS Postgres + ElastiCache Redis (`publicly_accessible=false`) |
| `loadbalancer` | ALB 443 + TGs + path rules |
| `storage` | S3 weights/idle/replays (+ tfstate bucket if not pre-existing) |
| `secrets` | SSM parameters placeholders |
| `monitoring` | CW log groups, billing alarms $50/$100, SNS email |

Roots only: `infra/environments/{global,dev,prod}`. State: S3 + DynamoDB lock.

**No modules for:** NAT, private subnet, Route53, WAF, ECR, Secrets Manager.

### WP2 — Environment roots

| Env | Notes |
|---|---|
| `global` | OIDC provider, shared IAM |
| `dev` | Spot, single-AZ, cost ~MVP sheet |
| `prod` | same topology; OD/Multi-AZ via tfvars later |

### WP3 — Docker images (`services/`)

Dirs already exist empty — fill them:

| Dir | Image role | Arch | Entrypoint |
|---|---|---|---|
| `services/backend` | FastAPI/Pipecat API | arm64 | no large weights |
| `services/llm` + `services/tts` | 2 separate images, 2 containers in 1 shared GPU Task | amd64 GPU | `aws s3 sync` then vLLM / Omni |
| `services/avatar` | avatar-server + LiveKit publish | amd64 GPU | sync weights + start |
| `services/livekit` | SFU | arm64 | config + start |
| `services/lmcache` | lmcache-server | arm64 | start ZMQ :5555 |
| `services/scripts` | entrypoint helpers (`fetch_weights.sh`) | — | — |

Rules: multi-stage; **no weights in layers**; health endpoints mandatory.

### WP4 — GitHub Actions

| File | Trigger | Action |
|---|---|---|
| `ci.yml` | PR / push | lint + test + docker build check — **no deploy** |
| `deploy-dev.yml` | `develop` | Hub `dev-*` → ECS dev |
| `deploy-prod.yml` | tag `v*` on `main` | approve → Hub `v*`/`latest` → ECS prod |

OIDC trust: repo + ref. Optional: fail if SG opens `0.0.0.0/0` on non-443/non-UDP-media.

### WP5 — ECS wiring smoke

- Task defs reference Hub images + SSM + S3 env  
- `LMCACHE_ENABLED` → desired_count 0/1  
- GPU: only llm container declares GPU; tts shares UUID  
- LiveKit UDP 50000-60000  

## Acceptance

- [ ] `terraform plan` clean for `environments/dev`  
- [ ] 8 modules have main/variables/outputs/README  
- [ ] Dockerfiles build in CI without weights  
- [ ] `ci.yml` green  
- [ ] `deploy-dev` updates ≥1 service via OIDC  
- [ ] No NAT/ECR/WAF/Secrets Manager in state  

## Dependency on Plan 01

| 00 needs from 01 | When |
|---|---|
| Real backend code that boots with remote URLs | before useful deploy-dev |
| Avatar mock LiveKit publisher | before media E2E |
| Health/ready semantics | before ALB health checks |

Infra modules can land **before** Wave A code; deploy-dev **after** Wave A/B images exist.

## Progress

| WP | Status |
|---|---|
| WP0 contracts | not started |
| WP1 modules | not started |
| WP2 env roots | not started |
| WP3 Docker | not started (dirs empty) |
| WP4 GHA | not started |
| WP5 smoke | not started |
