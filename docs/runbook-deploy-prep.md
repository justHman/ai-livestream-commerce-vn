# Runbook: Deploy-prep (AWS Seoul MVP)

Offline checklist before first real deploy. No `terraform apply` in this doc —
only bootstrap order, secrets, images, OIDC, and smoke commands.

Region: `ap-northeast-2`. Images: Docker Hub public `justhman/*`. Weights: S3.
Secrets: SSM SecureString (not Secrets Manager). No NAT / ECR / WAF for MVP.

## 0. Preconditions

- [ ] AWS account with billing alarm
- [ ] IAM user/role able to create OIDC provider + deploy roles
- [ ] Docker Hub account `justhman` (public repos for backend/llm/tts/avatar/livekit/lmcache)
- [ ] GitHub repo with Environments: `development`, `production` (prod requires reviewers)
- [ ] Domain optional (Cloudflare or raw ALB DNS)

## 1. Secrets inventory

Put values in **SSM SecureString** under `/ai-live/{env}/...` (names illustrative).
GitHub **Actions secrets** only hold OIDC role ARNs + Docker Hub login.

### GitHub Actions secrets

| Secret | Used by | Notes |
|--------|---------|-------|
| `AWS_ROLE_ARN_DEV` | deploy-dev | OIDC role for develop → DEV |
| `AWS_ROLE_ARN_PROD` | deploy-prod | OIDC role for tag `v*` → PROD |
| `DOCKERHUB_USER` | deploy-* | public push |
| `DOCKERHUB_TOKEN` | deploy-* | PAT with write on `justhman/*` |

### SSM / runtime (backend task env)

| Name | Example | Required |
|------|---------|----------|
| `APP_ENV` | `dev` / `prod` | yes |
| `DATABASE_URL` | `postgresql://...@...:5432/runtime` | yes (runtime DB) |
| `REDIS_URL` | `redis://...:6379/0` | yes |
| `LLM_BASE_URL` | `http://llm:8001` | yes (remote) |
| `TTS_BASE_URL` | `http://tts:8002` | yes (remote) |
| `AVATAR_BASE_URL` | `http://avatar:8080` | yes (remote_avatar) |
| `LIVEKIT_URL` | `wss://...` or `ws://livekit:7880` | media |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | from LiveKit config | media |
| `BACKEND_API_TOKEN` / `ADMIN_API_TOKEN` | long random | prod auth |
| `LIVEAVATAR_API_KEY` | only if `RENDER_BACKEND=cloud` | optional |
| `LMCACHE_ENABLED` | `false` until scale needed | optional |
| `PIPECAT_ENABLED` | `false` until Wave C full | optional |
| `LIVEKIT_PUBLISH` | `false` until publisher SDK wired | optional |

Never commit real values. `.env.example` is the offline template only.

## 2. Terraform bootstrap order

Run from `infra/environments/*` roots only (see `docs/terraform-layout.md`).

1. **global** (once per account)
   - S3 tfstate bucket + DynamoDB lock table
   - GitHub OIDC provider (`token.actions.githubusercontent.com`)
   - Deploy roles trusted for `repo:OWNER/REPO:ref:refs/heads/develop` and tags
2. **dev**
   - network (2 public AZs, no NAT) → security → storage → secrets → database
   - loadbalancer → compute (ECS + GPU ASG) → monitoring
3. **prod** (after DEV smoke)
   - same modules, stricter deletion protection / multi-AZ as tfvars allow

Typical commands (after AWS creds for an admin role):

```bash
cd infra/environments/global
terraform init
terraform plan
# terraform apply   # only when account is ready

cd ../dev
terraform init
terraform plan
# terraform apply
```

Capture outputs: ALB DNS, RDS endpoint, Redis endpoint, ECS cluster name.

## 3. Docker Hub images

Build from **repo root** (`implementations/`). Prefer `--platform` matching service arch.

| Image | Dockerfile | Arch |
|-------|------------|------|
| `imimjusthman/ai-live-backend` | `services/backend/Dockerfile` | arm64 |
| `imimjusthman/ai-live-llm` | `services/llm/Dockerfile` (or llm-tts) | amd64+gpu |
| `imimjusthman/ai-live-tts` | `services/tts/Dockerfile` | amd64+gpu |
| `imimjusthman/ai-live-avatar` | `services/avatar/Dockerfile` | amd64+gpu |
| `imimjusthman/ai-live-livekit` | `services/livekit/Dockerfile` | arm64 |
| `imimjusthman/ai-live-lmcache` | `services/lmcache/Dockerfile` | arm64 |

```bash
docker login -u justhman
docker build -f services/backend/Dockerfile -t imimjusthman/ai-live-backend:dev .
docker push imimjusthman/ai-live-backend:dev
# repeat per service; CI deploy-dev does this on push to develop when secrets exist
```

Weights: upload to S3 `s3://ai-livestream-{env}/weights/...`; entrypoints pull via `WEIGHTS_S3_URI`.

## 4. OIDC deploy path

1. Create IAM OIDC provider for GitHub (global module).
2. Role trust: `sub` restricted to `repo:justHman/<repo>:ref:refs/heads/develop` (dev)
   and tag/ref for prod.
3. Role policy: ECS update-service, ECR-not-used, ECR skip; S3 weights read; SSM get params;
   CloudWatch logs.
4. Set `AWS_ROLE_ARN_DEV` / `AWS_ROLE_ARN_PROD` in GitHub secrets.
5. Flip `if: false` stubs in `.github/workflows/deploy-dev.yml` / `deploy-prod.yml`
   once cluster + ALB exist.

## 5. First smoke commands

### Local / CI (offline, always)

```bash
pip install -e .
RENDER_BACKEND=mock LLM_ENGINE=none TTS_ENGINE=tone APP_ENV=dev \
  pytest core/tests/ -q
```

### After DEV ALB is up

```bash
# Health
curl -fsS "https://<alb-or-dev-api>/api/v1/health/live"
curl -fsS "https://<alb-or-dev-api>/api/v1/health/ready"

# Auth (when tokens set)
curl -fsS -H "Authorization: Bearer $BACKEND_API_TOKEN" \
  "https://<alb-or-dev-api>/api/v1/engines"

# LiveKit room token mint
curl -fsS -X POST -H "Authorization: Bearer $BACKEND_API_TOKEN" \
  "https://<alb-or-dev-api>/api/v1/media/livekit/room/smoke-1"

# Session start (lite path still supported)
curl -fsS -X POST -H "Authorization: Bearer $BACKEND_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"is_sandbox":true}' \
  "https://<alb-or-dev-api>/api/v1/lite/start"
```

### FE

1. Open `frontend/lite.html`.
2. Backend URL = ALB / Cloudflare URL.
3. Paste viewer token if prod auth on.
4. Start session → MOCK keeps MJPEG; if `livekit_url` + token present, Connect LiveKit
   (or auto-connect when CDN loads).

### Schema apply (runtime DB)

```bash
# From a bastion / ECS Exec one-shot with DATABASE_URL
psql "$DATABASE_URL" -f core/sql/runtime_schema.sql
```

Or call `PostgresRuntimeStore.apply_schema()` once from an admin job.

## 6. Feature flags for first deploy

| Flag | First deploy value | Why |
|------|--------------------|-----|
| `RENDER_BACKEND` | `remote_avatar` or `mock` | cloud LiveAvatar optional |
| `SESSION_STORE` | `redis` | multi-task backend |
| `LMCACHE_ENABLED` | `false` | desired_count 0 on lmcache ASG |
| `PIPECAT_ENABLED` | `false` | StreamOrchestrator until Wave C |
| `LIVEKIT_PUBLISH` | `false` | publisher stub until SDK in image |
| `DEBUG_ENABLED` | `false` in prod | hide mock traffic routes |

## 7. Rollback sketch

1. ECS: redeploy previous image tag (`imimjusthman/ai-live-backend:<old-sha>`).
2. Terraform: avoid destructive DB changes on first week; prefer additive schema only.
3. Feature flags: set `LMCACHE_ENABLED=false`, `LIVEKIT_PUBLISH=false` without rebuild.

## 8. Done when

- [ ] `pytest core/tests/ -q` green in CI
- [ ] global + dev terraform plan clean (apply deferred to account ready)
- [ ] at least backend image on Docker Hub
- [ ] OIDC role ARNs in GitHub secrets
- [ ] `/health/live` 200 on DEV ALB
- [ ] lite.html start session works (MJPEG and/or LiveKit)
