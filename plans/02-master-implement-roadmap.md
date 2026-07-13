# Master Implement Roadmap — From As-Is Code → Confirmed Architecture

> Status: **PLAN ONLY — do not implement until user approves this report** (2026-07-11).  
> Sources of truth (confirmed):
> - `docs/brief-for-confirmation.md`
> - `docs/scope-engine-and-models.md`
> - `docs/aws-architecture.md`
> - `docs/terraform-layout.md`
> - `docs/cicd-branch-strategy.md`
> - `docs/aws-pricing-seoul.csv` (cost guardrails; MVP ~$699/mo Spot LMCache ON)
>
> Execution plans (children of this roadmap):
> - `00-implement-aws-stack.md` — infra/Docker/CI only
> - `01-app-feature-backlog.md` — app/service code migration + features

---

## 0. CodeGraph

| Item | Value |
|---|---|
| Project | `implementations/` |
| CLI | codegraph 1.2.0 |
| Index | **91 files**, 1,426 nodes, 3,555 edges |
| Sync | `codegraph sync` → already up to date |
| Force reindex | **blocked EPERM** — MCP server holds `\.codegraph\codegraph.db` lock |
| Action for user | Reconnect/restart CodeGraph MCP in Claude Code, then run `codegraph index --force` once if full rebuild wanted |

Index is usable for exploration now; force rebuild not required for planning.

---

## 1. Where the project is today (as-is)

### 1.1 What exists and works (P0/P1 monolith)

| Area | Path | State |
|---|---|---|
| FastAPI app factory | `core/server.py` `create_app()` | DONE — CORS gate, EngineManager, DirectorCoordinator wiring |
| API surface | `core/api/v1.py` prefix `/api/v1` | DONE but **legacy naming** |
| Auth | `core/api/auth.py` | DONE — viewer/admin tokens, WS token |
| Render seam | `core/render/base.py` | DONE — `FullPipelineBackend` / `StreamingAvatarBackend` |
| Cloud avatar | `core/render/cloud.py` + `providers/liveavatar_cloud/` | DONE — LiveAvatar cloud |
| Mock avatar | `core/render/mock.py` | DONE — idle loop + MJPEG |
| Self-host avatar | `core/render/self_host.py` | **STUB** `NotImplementedError` |
| Stream pipeline | `core/render/orchestrator.py` | DONE — LLM→chunker→TTS→video queue (in-process) |
| Director | `core/director/*` | DONE — FSM OPENING/SELLING/CLOSING, ChatQueue, Coordinator 300ms tick |
| LLM adapters | `core/llm/adapters/{vllm,sglang,transformers}.py` | In-process loaders (no remote OpenAI-compat client path as default) |
| TTS adapters | `core/tts/adapters/{vieneu,cosyvoice,transformers}.py` | In-process; presets still Colab-style |
| Session store | `core/store.py` | memory \| redis **KV only** — no Postgres runtime schema |
| Frontend | `frontend/lite.html` | Demo against `/lite/*` + MJPEG |
| Tests | `core/tests/*` | Large offline suite |
| Notebook | `notebooks/colab_demo.ipynb` | Colab demo path |

### 1.2 What is empty / missing (infra + multi-service)

| Path | State |
|---|---|
| `services/{backend,llm-tts,avatar,livekit,lmcache,scripts}/` | **Empty dirs only** — no Dockerfile, no entrypoint |
| `infra/` | **Does not exist** |
| `.github/workflows/` | **Does not exist** |
| Root `pyproject.toml` / `requirements.txt` | **Missing** (only `providers/liveavatar_cloud/requirements.txt`) |
| Pipecat | **Not in code** |
| Outlines / guided decoding | **Not wired** |
| `POST .../plan/create` | **Missing** |
| LiveKit self-host SFU + publish tracks | **Not implemented** (cloud LiveAvatar path only) |
| Postgres runtime DB | **Missing** |
| HTTP/SSE clients to remote LLM/TTS/Avatar | **Missing** (engines load in-process) |
| LMCache client/server packaging | **Missing** |

### 1.3 Current API map (as-is)

```
/api/v1/health|/health/live|/health/ready
/api/v1/lite/{start,say,interrupt,stop,attach,ingest,chat}
/api/v1/engines[|/llm|/tts|/tts/preset]
/api/v1/ws/control/{session_id}
/api/v1/mock/{frame,video,status}/...
/api/v1/debug/*
```

### 1.4 Confirmed target API map (brief §M)

```
/api/v1/sessions[|/{id}/attach|stop|say|plan/create]
/api/v1/avatars[|/{id}|/{id}/idle/regenerate]
/api/v1/ws/platform/{sid}
/api/v1/ws/control/{sid}
/api/v1/media/livekit/room/{sid}
/api/v1/media/frame/{sid}.png          # debug only
/api/v1/engines[|/tts|/llm]
/api/v1/health/{ready,live}
/api/v1/admin/*
```

### 1.5 Architecture delta (one picture)

```
AS-IS (monolith process)
  FE ──HTTP/WS──► core FastAPI
                    ├─ EngineManager loads LLM+TTS in-process
                    ├─ StreamOrchestrator in-process
                    ├─ Mock MJPEG OR LiveAvatar cloud
                    └─ memory/redis session KV

TARGET (confirmed multi-service, Seoul)
  CF Free ──► ALB ──► Backend Fargate (Pipecat + API, ARM Spot)
                 │         ├─ HTTP/SSE ──► LLM container (vLLM AWQ on g6)
                 │         ├─ HTTP/SSE ──► TTS container (vLLM-Omni VieNeu on same g6 Task)
                 │         ├─ HTTP ──────► Avatar container (LiveKit video publish on g4dn)
                 │         ├─ Redis streams/locks
                 │         └─ RDS Postgres runtime
                 ├─ LiveKit SFU Fargate (media UDP public)
                 └─ LMCache c7g Spot (optional LMCACHE_ENABLED)
  Images: Docker Hub public | Weights: S3 via entrypoint | Secrets: SSM
```

**Conclusion:** Codebase is a **working control-plane monolith + cloud/mock renderer**. Confirmed design is a **service split + AWS lift**. Planning must treat this as **migration**, not greenfield rewrite of Director/auth/tests from zero.

---

## 2. Non-negotiables (locked — do not re-debate)

| Decision | Value |
|---|---|
| Region | `ap-northeast-2` Seoul |
| Network | 1 public subnet, IGW, **S3 Gateway Endpoint**, **no NAT** |
| Edge | Cloudflare Free → ALB Full(strict) |
| Registry | Docker Hub **public**; weights on S3 |
| Secrets | SSM SecureString (not Secrets Manager MVP) |
| LLM | vLLM 0.22 + `cyankiwi/Qwen3.5-4B-AWQ-4bit` remote serve |
| TTS | vLLM-Omni fork + VieNeu-v2 remote serve |
| Avatar | half/full-body only; mock first on avatar-server; no head-only prod path |
| Media | **LiveKit from day 1**; MJPEG debug-only |
| Orchestration | **Pipecat** on backend (not hand StreamOrchestrator long-term) |
| Structured out | **Outlines** Utterance schema |
| Cost MVP | ~$699/mo Spot LMCache ON (validated sheet) |

Rejected forever for MVP: NAT, private subnet, ECR, Route53, AWS WAF, Secrets Manager, API Gateway, bake weights into image, llama.cpp as AWS prod LLM.

---

## 3. Gap matrix (as-is → target)

| # | Gap | Severity | Belongs to plan |
|---|---|---|---|
| G1 | No Terraform / no VPC-ALB-ECS-RDS-Redis | Blocker AWS | **00** |
| G2 | Empty `services/*` (no Docker/entrypoints) | Blocker AWS | **00** |
| G3 | No GHA CI/CD OIDC | Blocker deploy | **00** |
| G4 | Engines in-process, not HTTP/SSE remote clients | Blocker multi-service | **01** (Wave A) |
| G5 | No LiveKit SFU + track publish (API audio / avatar video) | Blocker media | **01** + **00** LiveKit image |
| G6 | StreamOrchestrator not Pipecat | High (confirmed) | **01** |
| G7 | No Outlines / Utterance schema | High | **01** |
| G8 | No run-plan `plan/create` + cursor + coverage | High | **01** |
| G9 | API still `/lite/*` not `/sessions/*` + missing `/avatars` `/media` `/ws/platform` | High | **01** |
| G10 | No Postgres runtime schema/store | High | **01** (+ RDS in **00**) |
| G11 | Redis only session KV — need ChatQueue XADD + locks multi-instance | Medium | **01** |
| G12 | LLM presets GGUF/Colab-first; prod model not default | Medium | **01** |
| G13 | TTS presets in-process; prod Omni not default | Medium | **01** |
| G14 | Self-host avatar stub; mock not LiveKit-published | Medium | **01** Phase F later |
| G15 | LMCache not packaged/toggled | Medium | **00** service + **01** env flag |
| G16 | No root dependency lock for backend image | Medium | **00**/**01** |
| G17 | Cost numbers in brief §F still ~$662 — sheet is $699 | Doc | fix on next doc touch (non-blocking) |
| G18 | `services/` empty placeholders exist — good skeleton for Docker | Info | **00** |

---

## 4. Implementation strategy (recommended order)

### Principle

1. **Keep** Director / auth / tests / seams — migrate callers, do not rewrite FSM from scratch.  
2. **Split** LLM/TTS/Avatar out of process **before** heavy Pipecat polish (clients first, then swap orchestrator).  
3. **Infra in parallel** with app Wave A once Docker contracts (ports/env/health) are frozen.  
4. **No implement until this roadmap approved.**

### Phase map

```
Phase 0   Freeze contracts (ports, env, health, image names)     [docs only]
Phase 1   Plan 00 — AWS foundation (network→data→compute skeleton)
Phase 2   Plan 00 — Docker skeletons + CI build-only
Phase 3   Plan 01 Wave A — remote engine clients + API rename facade
Phase 4   Plan 01 Wave B — LiveKit media path (mock avatar publish)
Phase 5   Plan 01 Wave C — Pipecat + Outlines + run-plan
Phase 6   Plan 00 — wire ECS task defs to real images + deploy-dev
Phase 7   Plan 01 Wave D — Postgres runtime + Redis streams HA
Phase 8   Plan 01 Wave E — LMCache toggle + scale flags
Phase 9   Plan 01 Wave F — AvatarForcing/EchoAvatar benches (GPU)
Phase 10  Handoff docs D1–D8 (brief §4b) after runtime works
```

Infra (00) and app (01) **interleave** after Phase 0; they are not strictly serial for all work packages, but **deploy-dev** waits for Wave A+B images.

---

## 5. Phase 0 — Freeze contracts (before any code)

Write once into `docs/` or `plans/contracts.md` (on implement start):

| Service | Port(s) | Health | Image (Hub) | Arch |
|---|---|---|---|---|
| backend | 8800 | `/api/v1/health/live`, `/ready` | `justhman/ai-live-backend` | arm64 |
| llm | 8001 | vLLM `/health` | `justhman/ai-live-llm` | amd64+GPU |
| tts | 8002 | Omni health | `justhman/ai-live-tts` (or combined llm-tts Task) | amd64+GPU |
| avatar | 8080 | `/health` | `justhman/ai-live-avatar` | amd64+GPU |
| livekit | 7880 + UDP 50000-60000 | LiveKit health | `justhman/ai-live-livekit` or official image | arm64 |
| lmcache | 5555 ZMQ + 8080 metrics | metrics | `justhman/ai-live-lmcache` | arm64 |

ECS Task note (confirmed): **LLM+TTS = 2 containers / 1 Task / 1 GPU** on g6; only LLM declares GPU resource; TTS shares via `NVIDIA_VISIBLE_DEVICES`.

Env contract (minimum):

```
# backend
APP_ENV, LLM_BASE_URL, TTS_BASE_URL, AVATAR_BASE_URL, LIVEKIT_URL,
REDIS_URL, DATABASE_URL, LMCACHE_ENABLED, SSM-injected secrets

# llm
MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit
ENABLE_PREFIX_CACHING=1
GPU_MEMORY_UTILIZATION=0.6
GUIDED_DECODING=outlines   # when Wave C

# tts
MODEL_ID=pnnbao-ump/VieNeu-TTS-v2
GPU_MEMORY_UTILIZATION=0.25

# weights
WEIGHTS_S3_URI=s3://ai-livestream-{env}/weights/...
```

---

## 6. Plan 00 detail — AWS stack (refined)

See also `00-implement-aws-stack.md` (updated to match this roadmap).

### 6.1 WP order (00)

| Order | WP | Deliverable | Exit criteria |
|---|---|---|---|
| 00.1 | Contracts | ports/env/image names committed | Phase 0 done |
| 00.2 | `modules/network` | VPC, 1 public subnet, IGW, S3 GW EP | plan shows 0 NAT |
| 00.3 | `modules/security` | SG matrix §3 aws-architecture; OIDC; IAM; IMDSv2 | no :22; CI policy check |
| 00.4 | `modules/storage` + `secrets` + `monitoring` | S3, SSM placeholders, CW+SNS billing | apply-able |
| 00.5 | `modules/database` | RDS t4g.medium SA + Redis t4g.small | no public IP |
| 00.6 | `modules/loadbalancer` | ALB 443 + path rules | TG placeholders |
| 00.7 | `modules/compute` | ECS cluster, CP Fargate Spot + EC2 Spot GPU ASG, LMCache ASG, 4 services | desired_count vars |
| 00.8 | `environments/{global,dev,prod}` | roots + backend.tf S3/DDB | `terraform plan` clean |
| 00.9 | Dockerfiles skeleton | 4–5 images multi-stage + `aws s3 sync` entrypoint | build in CI |
| 00.10 | GHA `ci.yml` | lint/test/docker build — no deploy | green on PR |
| 00.11 | GHA `deploy-dev.yml` | OIDC → Hub `dev-*` → ECS | after Wave A/B images |
| 00.12 | GHA `deploy-prod.yml` | tag `v*` + approve | later |

### 6.2 Explicit non-modules

NAT, private subnet, Route53, WAF, ECR, Secrets Manager — **never added** in MVP PRs.

---

## 7. Plan 01 detail — App migration waves

See also `01-app-feature-backlog.md` (updated).

### Wave A — Remote engines (replace in-process default for AWS)

| Task | Work | Keep |
|---|---|---|
| A1 | `core/llm/adapters/openai_compat.py` (or extend vllm) — client to `LLM_BASE_URL` SSE | LLMEngine ABC |
| A2 | `core/tts/adapters/openai_omni.py` — client to TTS Omni stream | TTSEngine ABC |
| A3 | Avatar HTTP client: `start_speak` / `stop` / health | StreamingAvatarBackend |
| A4 | Config: `LLM_BASE_URL`, `TTS_BASE_URL`, `AVATAR_BASE_URL`; default prod engine=`remote` | env pattern |
| A5 | Deprecate in-process as AWS default; keep for Colab/dev (`LLM_ENGINE=llamacpp|hf`) | dual mode |
| A6 | Root `pyproject.toml` / lock for backend image | — |

**Exit:** Backend container talks to mock HTTP LLM/TTS stubs in compose-or-process tests without loading GPU weights in-process.

### Wave B — LiveKit media (kill MJPEG as primary)

| Task | Work |
|---|---|
| B1 | LiveKit server image/config (dev + ECS) |
| B2 | Avatar service: LiveKit SDK publish **video** track; idle loop 75 frames @25fps into VideoSource |
| B3 | Backend/Pipecat later publishes **audio** track; interim: backend can publish audio via simple worker |
| B4 | `POST /api/v1/media/livekit/room/{sid}` token mint |
| B5 | FE: LiveKit JS subscribe (lite.html path) |
| B6 | Keep `/mock/*` behind `DEBUG_ENABLED` only |

**Exit:** FE sees continuous video from LiveKit with idle loop; no black screen.

### Wave C — Pipecat + Outlines + run-plan

| Task | Work | Task # |
|---|---|---|
| C1 | Pipecat pipeline replaces StreamOrchestrator path for remote engines | #54 |
| C2 | Custom Pipecat TTS service wrapper for Omni | #54 |
| C3 | vLLM `--guided-decoding-backend outlines` + Utterance schema | #55 |
| C4 | `POST /sessions/{id}/plan/create` + RunPlan schema | #60 |
| C5 | Director cursor `{phase, product_idx, talking_point_idx}` + coverage via BiEncoder | #60 |
| C6 | Reactive > proactive decision order per brief §D | #60 |

**Exit:** One session: plan create → attach → chat flood → structured utterances → audio/video tracks.

### Wave D — Data plane

| Task | Work |
|---|---|
| D1 | Postgres schema: sessions, session_products snapshot, viewer_msgs, director_decisions, llm/tts logs, audit |
| D2 | `asyncpg` store; pgvector optional for product embeddings |
| D3 | Redis: ChatQueue `XADD`, session locks, ownership for multi-instance |
| D4 | LISTEN/NOTIFY or hub keep WS fanout |

### Wave E — Scale flags

| Task | Work | Task # |
|---|---|---|
| E1 | `LMCACHE_ENABLED` toggles lmcache service desired_count + vLLM kv-transfer-config | #47 #56 |
| E2 | Document scale metrics: `num_requests_waiting`, `gpu_cache_usage_perc` | brief §I |

### Wave F — Avatar models (research/bench, not MVP gate)

| Task | Work | Task # |
|---|---|---|
| F1 | AvatarForcing T4/L4 bench | #51 |
| F2 | EchoAvatar license + L4 spike | #52 |
| F3 | AWQ INT4 vs INT8-INT4 | #48 |
| F4 | Wire winning model into avatar-server StreamingAvatarBackend | — |

### Wave G — API rename + team boundary

| Task | Work |
|---|---|
| G1 | Alias or migrate `/lite/*` → `/sessions/*` (compat window) |
| G2 | Add `/avatars/*`, `/ws/platform/{sid}`, `/admin/*` |
| G3 | Do **not** implement `/user/*` `/shop/*` (team SE) |

---

## 8. What to reuse vs replace

| Reuse as-is | Adapt | Replace / new |
|---|---|---|
| Director FSM, cluster, scorer, embedder, ChatQueue, Coordinator tick idea | Coordinator speaks via Pipecat/remote | In-process StreamOrchestrator as prod path |
| Auth + app factory pattern | Middleware list from aws-architecture §11 | — |
| RenderBackend ABC split | Self-host implementations | Cloud-only assumption for prod media |
| Mock idle loop math | Move into avatar-server LiveKit publisher | MJPEG as primary FE path |
| Offline pytest style | Add contract tests for HTTP engines | GPU-in-process as default CI |
| `providers/liveavatar_cloud` | Optional RENDER_BACKEND=cloud | Not default AWS avatar |

---

## 9. Risk register

| Risk | Mitigation |
|---|---|
| Omni+VieNeu only verified Colab T4, not g6 L4 Seoul | Smoke on single g6 Spot early (Wave A image) |
| GPU share 0.6/0.25 OOM | Measure TTS footprint; adjust util; separate tasks only if needed |
| Spot reclaim mid-stream | ECS replace + FE `reconnecting`; prod later OD |
| Pipecat effort overrun | Wave A remote clients first; Pipecat can wrap same clients |
| API rename breaks FE/tests | Dual routes compat 1 milestone |
| Empty root deps | Add pyproject before first Docker backend build |
| CodeGraph force index locked | Restart MCP then `index --force` |

---

## 10. Definition of Done (program)

**MVP AWS demo (minimum shippable):**

1. `terraform apply` dev: network + SG + ALB + ECS + RDS + Redis + S3 + SSM  
2. Four services healthy; backend ARM Spot; LLM+TTS on g6 Spot; avatar mock on g4dn Spot; LiveKit up  
3. FE: start session → LiveKit video idle → chat → speech+lip/mock frames  
4. Cost surface still no NAT/ECR/WAF  
5. CI green; deploy-dev works via OIDC  

**Not required for MVP demo:** EchoAvatar/AvatarForcing prod quality, Multi-AZ, Outlines polish 100%, team SE `/user`/`/shop`.

---

## 11. Suggested first implement PR sequence (after approval)

1. **PR-docs:** contracts + this roadmap frozen (no runtime)  
2. **PR-infra-1:** `infra/modules/network` + `security` + `storage` + `secrets` + `monitoring` + `environments/dev` skeleton  
3. **PR-docker-1:** `services/backend` Dockerfile + root pyproject + health-only app boot  
4. **PR-app-A1:** remote LLM/TTS clients + config  
5. **PR-infra-2:** database + loadbalancer + compute placeholders  
6. **PR-docker-2:** llm-tts + avatar-mock + livekit  
7. **PR-app-B:** LiveKit publish path  
8. **PR-ci:** `ci.yml` + `deploy-dev.yml`  
9. **PR-app-C:** Pipecat + Outlines + plan/create  
10. **PR-app-D:** Postgres runtime  

---

## 12. Report checklist for user approval

- [x] CodeGraph status known (sync OK; force reindex needs MCP unlock)  
- [x] As-is codebase mapped  
- [x] Confirmed arch delta explicit  
- [x] Gaps G1–G18 listed  
- [x] Phase order 0–10  
- [x] Plan 00 / 01 scopes separated  
- [x] Reuse vs replace  
- [x] First PR sequence  
- [ ] **User approves → only then implement**

---

## 13. Immediate asks for user (only if blocking)

None required to start Phase 0–1 after approval. Optional:

1. Docker Hub namespace (`justhman/...` assumed).  
2. Domain already on Cloudflare? (can use ALB DNS temporarily).  
3. Restart CodeGraph MCP so `index --force` can run.
