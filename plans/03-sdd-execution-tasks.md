# SDD Execution Tasks — Implement AWS MVP foundation

> Parent: `02-master-implement-roadmap.md` (user-approved).  
> Style: Subagent-Driven Development. Independent domains may run **in parallel** if they touch **disjoint paths**.  
> Workdir: `implementations/` (nested git repo). Branch: `feature/implement-aws-mvp`.

## Global constraints

1. Region `ap-northeast-2`. Public subnet only. **No NAT, private subnet, ECR, Secrets Manager, Route53, AWS WAF modules.**
2. S3 Gateway Endpoint KEEP. Cloudflare Free edge. Docker Hub public. Weights on S3 via entrypoint. SSM SecureString.
3. LLM+TTS = 2 containers / 1 ECS Task / 1 GPU (g6). Backend+LiveKit = Fargate Spot ARM.
4. Do not rewrite Director FSM. Do not break offline pytest suite (`python -m pytest core/tests/ -q`).
5. Commit after each task; message prefix `feat(infra)|feat(docker)|feat(ci)|feat(core)|docs:`.
6. No `Co-Authored-By` trailers.
7. Never commit `.env` secrets.
8. Cost guardrail: no service that reintroduces NAT/ECR/WAF.

## Parallelism rules

| Domain | Owned paths | Parallel with |
|---|---|---|
| D-infra | `infra/**` only | D-docker, D-contracts |
| D-docker | `services/**` only | D-infra, D-contracts |
| D-contracts | `plans/contracts.md`, `pyproject.toml`, `uv.lock` if any, `.github/**` | D-infra, D-docker |
| D-core | `core/**`, `frontend/**` | **SERIAL** after foundation; never parallel with another D-core task |

---

## Task 1 — Freeze service contracts

**Domain:** D-contracts  
**Files:** create `plans/contracts.md`

Write the port/env/image contract table exactly:

| service | port | health | hub image | arch |
|---|---|---|---|---|
| backend | 8800 | `/api/v1/health/live`, `/api/v1/health/ready` | `imjusthman/ai-live-backend` | arm64 |
| llm | 8001 | `/health` | `imjusthman/ai-live-llm` | amd64+gpu |
| tts | 8002 | `/health` | `imjusthman/ai-live-tts` | amd64+gpu |
| avatar | 8080 | `/health` | `imjusthman/ai-live-avatar` | amd64+gpu |
| livekit | 7880 + UDP 50000-60000 | `/` or livekit health | `imjusthman/ai-live-livekit` | arm64 |
| lmcache | 5555 zmq + 8080 metrics | `:8080/metrics` | `imjusthman/ai-live-lmcache` | arm64 |

Include minimum env vars from master roadmap §5. No code.

**Test:** file exists; table has 6 services.  
**Commit:** `docs: freeze service contracts for AWS multi-service stack`

---

## Task 2 — Root pyproject for backend image

**Domain:** D-contracts  
**Files:** `pyproject.toml` at implementations root (create if missing)

- name: `ai-livestream-backend`
- python `>=3.11`
- deps minimal for current core offline tests: fastapi, uvicorn, pydantic, httpx, python-multipart, redis (optional extra), pytest, pytest-asyncio
- Do NOT pin torch/vllm in root (GPU images own those)
- `[project.scripts]` optional none

**Test:** `python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"` or parse OK; `pytest core/tests/test_app_factory.py -q` still collectable if env allows.  
**Commit:** `feat(core): add root pyproject for backend packaging`

---

## Task 3 — GitHub Actions ci.yml

**Domain:** D-contracts  
**Files:** `.github/workflows/ci.yml`

- on: pull_request + push to `develop`/`main`/`feature/**`
- jobs: 
  - `test`: checkout, setup-python 3.11, install pyproject/deps, `pytest core/tests/ -q --ignore=...` offline
  - `docker-build`: matrix backend Dockerfile build only (no push) if file exists
- No AWS deploy keys
- No deploy job

**Test:** YAML valid (parse).  
**Commit:** `feat(ci): add offline test and docker build workflow`

---

## Task 4 — Terraform network + security + storage + secrets + monitoring

**Domain:** D-infra  
**Files:** `infra/modules/{network,security,storage,secrets,monitoring}/**`

Per `docs/terraform-layout.md` + SG matrix `docs/aws-architecture.md` §3.

- network: VPC, 2 public subnets across AZs, IGW, public routes, **aws_vpc_endpoint** Gateway S3, tags env
- security: SG alb/backend/llm/tts/avatar/rds/redis/lmcache/livekit; variables for cloudflare optional; no :22; outputs sg ids
- storage: S3 bucket weights+idle+replays (or one bucket prefixes); block public ACL
- secrets: aws_ssm_parameter SecureString placeholders (overwrite false)
- monitoring: log groups, sns topic email var, billing alarm stubs

No provider block in children. variables.tf + outputs.tf + README.md each.

**Test:** `terraform fmt -check` if terraform installed; else python/hcl syntax sanity (files non-empty, required blocks present). Script `services/scripts/check_tf_layout.py` optional.  
**Commit:** `feat(infra): add network security storage secrets monitoring modules`

---

## Task 5 — Terraform database + loadbalancer modules

**Domain:** D-infra  
**Files:** `infra/modules/{database,loadbalancer}/**`

- database: RDS postgres 16 t4g.medium single-AZ, gp3 100GB, publicly_accessible=false; ElastiCache redis t4g.small; subnet group using public subnet ids (MVP)
- loadbalancer: ALB internet-facing, HTTPS listener optional (var certificate_arn empty → HTTP 80 for dev), target groups backend:8800, path rules

**Commit:** `feat(infra): add database and loadbalancer modules`

---

## Task 6 — Terraform compute module skeleton

**Domain:** D-infra  
**Files:** `infra/modules/compute/**`

- ECS cluster
- capacity providers: FARGATE_SPOT + EC2 Spot ASG placeholders (g6.xlarge, g4dn.xlarge, c7g.2xlarge lmcache)
- 4 task definition skeletons (backend, llm-tts family, avatar, lmcache) with image vars
- 4 services desired_count vars; lmcache desired_count default 0
- GPU resource on llm container only (document tts share)

**Commit:** `feat(infra): add compute ECS and ASG skeleton module`

---

## Task 7 — environments/dev root

**Domain:** D-infra  
**Files:** `infra/environments/dev/**`

- backend.tf (S3+DDB placeholders — use variables, document bootstrap)
- providers.tf ap-northeast-2
- main.tf wire all modules
- terraform.tfvars.example (not secrets)
- outputs.tf

**Commit:** `feat(infra): wire dev environment root module`

---

## Task 8 — Service Dockerfiles + entrypoints

**Domain:** D-docker  
**Files:** `services/**`

For each of backend, llm, tts (or llm-tts), avatar, livekit, lmcache:

- `Dockerfile` multi-stage
- `entrypoint.sh` for GPU services: `aws s3 sync` weights then exec
- backend: python slim, `uvicorn core.server:app --host 0.0.0.0 --port 8800`
- llm/tts: placeholder CMD that documents vllm serve (may use sleep/health stub if full vllm too heavy for CI — but structure real)
- livekit: based on livekit/livekit-server or document
- `.dockerignore` at root or per service

Also `services/scripts/fetch_weights.sh`.

**Test:** `docker build` backend if docker available; else Dockerfile exists + `hadolint` skip.  
**Commit:** `feat(docker): multi-stage Dockerfiles and weight entrypoints`

---

## Task 9 — Remote OpenAI-compat LLM client

**Domain:** D-core (SERIAL)  
**Files:** `core/llm/adapters/openai_compat.py`, register in `core/llm/__init__.py` / base ENGINES, `core/config.py` `LLM_BASE_URL`

- engine name: `openai_compat` or `remote`
- stream_chunks via httpx SSE to `{base}/v1/chat/completions`
- Works with vLLM OpenAI server
- Offline test with httpx mock / respx / manual mock transport

**Test:** `pytest core/tests/test_llm_remote_client.py -q` (new) + existing suite still green.  
**Commit:** `feat(core): add remote OpenAI-compat LLM engine client`

---

## Task 10 — Remote TTS client stub

**Domain:** D-core (SERIAL)  
**Files:** `core/tts/adapters/remote_http.py`, register engine, `TTS_BASE_URL`

- synthesize() GET/POST to TTS service; stream() if available
- Offline mock test

**Commit:** `feat(core): add remote HTTP TTS engine client`

---

## Task 11 — Config env for remote defaults documentation

**Domain:** D-core  
**Files:** `core/config.py`, `.env.example`

Document LLM_BASE_URL, TTS_BASE_URL, AVATAR_BASE_URL, LMCACHE_ENABLED without breaking defaults (dev still none/tone).

**Commit:** `feat(core): env knobs for remote engine URLs`

---

## Milestone 1 exit (after Tasks 1–11)

- [ ] contracts.md frozen  
- [ ] infra modules + dev root exist  
- [ ] Dockerfiles present  
- [ ] ci.yml present  
- [ ] remote LLM+TTS clients + tests  
- [ ] `pytest core/tests/ -q` green  

Then stop for optional user check OR continue Wave B LiveKit (Tasks 12+ in follow-up plan).

---

## Execution batching

**Batch P0 (parallel 3 agents):** Task 1+2+3 | Task 4 | Task 8  
**Batch P1 (infra serial or one agent):** Task 5 → 6 → 7  
**Batch P2 (core serial):** Task 9 → 10 → 11  
**Review:** per-task reviewer after each; whole-branch review after Milestone 1

---

## Milestone 2 — LiveKit media + deploy workflows (Tasks 12–15)

### Task 12 — GitHub Actions deploy-dev.yml + deploy-prod.yml skeleton

**Domain:** D-contracts  
**Files:** `.github/workflows/deploy-dev.yml`, `.github/workflows/deploy-prod.yml`

- `deploy-dev.yml`: on push to `develop`; permissions `id-token: write`, `contents: read`; OIDC role assume placeholder `AWS_ROLE_ARN_DEV`; build/push Hub tags `dev-*` for backend (and matrix if Dockerfiles exist); ECS update service placeholders (commented if secrets missing)
- `deploy-prod.yml`: on tag `v*`; `environment: production` with required reviewers note; no auto without approval
- No long-lived AWS keys
- Document required GitHub secrets/vars in workflow comments

**Commit:** `feat(ci): add deploy-dev and deploy-prod OIDC workflow skeletons`

### Task 13 — LiveKit room token endpoint

**Domain:** D-core  
**Files:** `core/livekit_tokens.py` (new), `core/api/v1.py`, `core/config.py`, tests

- Config: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (empty = feature off)
- `POST /api/v1/media/livekit/room/{session_id}` → returns `{livekit_url, token, room}` using livekit API JWT (implement with PyJWT or pure hmac if livekit sdk heavy — prefer minimal JWT HS256 per LiveKit access token spec)
- Auth: viewer_auth same as /lite/*
- If keys missing → 503 with clear message
- Test offline with fixed secret → decode claims

**Commit:** `feat(core): LiveKit room token mint endpoint`

### Task 14 — Remote Avatar HTTP client (StreamingAvatarBackend)

**Domain:** D-core  
**Files:** `core/render/remote_avatar.py` (new), retain as internal avatar-service HTTP client; public selectors remain explicit self-host model names, tests

- Implements StreamingAvatarBackend: start/stop/interrupt/stream_audio call `AVATAR_BASE_URL` HTTP
- stream_audio may POST audio window metadata; mock server in tests
- Do not break cloud/mock defaults

**Commit:** `feat(core): remote avatar HTTP StreamingAvatarBackend`

### Task 15 — Avatar service health app: idle loop MJPEG/LiveKit stub docs

**Domain:** D-docker (services/avatar only)  
**Files:** `services/avatar/health_app.py` expand to simple idle JPEG loop endpoint + README LiveKit publish steps

- Keep lightweight (no real LiveKit SDK required in image if not installed)
- `/health`, `/idle/frame.jpg` optional
- README documents how real LiveKit publish will plug in

**Commit:** `feat(docker): avatar service idle frame stub and LiveKit notes`

### Milestone 2 exit
- [ ] deploy workflows exist
- [ ] LiveKit token endpoint + tests green
- [ ] remote avatar backend + tests
- [ ] full pytest still green
