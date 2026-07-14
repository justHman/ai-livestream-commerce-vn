# Gap audit (code vs confirmed docs) + Milestone 3 plan

> Date: 2026-07-11. Re-verified 2026-07-14. Branch: `feature/implement-aws-mvp` (M3 code), `feature/m3-residual-gaps` (residual gaps).  
> Docs: brief, architecture, scope-engine, aws-architecture, terraform-layout, cicd-branch-strategy.

> **2026-07-14 re-verify note:** a full code read found four M3 rows below were
> stale — the code had advanced past what this audit recorded. The four
> re-verified rows: FE LiveKit subscribe (was TODO, actually DONE), MJPEG
> debug-only gate (was TODO, actually DONE), Backend audio LiveKit publish
> (was TODO, now DONE via `feature/m3-residual-gaps`), Postgres runtime store
> (was TODO, now DONE via `feature/m3-residual-gaps`).

## Status legend
- **DONE** — implemented + tests where applicable  
- **PARTIAL** — skeleton / flag / offline only  
- **TODO** — implement in M3  
- **DEFER** — needs GPU/account/real SFU; not deploy-prep code gate  

## Gap matrix

| Area | Doc requirement | Code now | Status | M3 action |
|---|---|---|---|---|
| Remote LLM HTTP/SSE | brief §D HTTP/SSE | `openai_compat` | DONE | — |
| Remote TTS HTTP | brief §B Omni | `remote_http` | DONE | tighten path docs |
| Remote avatar HTTP | brief §E | `remote_avatar` | DONE | — |
| LiveKit token | brief §M media | `POST .../media/livekit/room/{sid}` | DONE | — |
| LiveKit SFU image | aws-arch LiveKit Fargate | `services/livekit` | PARTIAL | polish config |
| Avatar idle LiveKit publish | brief §E idle 75@25 | idle JPEG only | PARTIAL | stub publisher module + README |
| Backend audio LiveKit publish | brief §E | `core/livekit_publish.py` real rtc path (lazy import + test seam) | DONE (2026-07-14) | — |
| FE LiveKit subscribe | brief FE | `frontend/lite.html` livekit-client CDN + `connectLiveKit()` dual MJPEG/LiveKit | DONE (re-verified 2026-07-14) | — |
| MJPEG debug-only | brief no MJPEG prod | `_mock_or_debug_allowed()` gates `/mock/*` + `/debug/*` | DONE (re-verified 2026-07-14) | — |
| API `/sessions/*` | brief §M | only `/lite/*` | TODO | aliases + thin session router |
| `/avatars/*` | brief §M | missing | TODO | CRUD in-memory MVP |
| `/ws/platform/{sid}` | brief §M | missing | TODO | WS accept + queue to ChatQueue |
| `/admin/*` | brief §M | debug only | TODO | admin health/config dump |
| plan/create + RunPlan | brief §D | missing | TODO | schema + endpoint + store on session |
| Director cursor + coverage | brief §D | phase only | TODO | cursor fields + biencoder coverage helper |
| Outlines Utterance | brief §D | missing | TODO | pydantic schema + openai_compat response_format/json schema option |
| Pipecat pipeline | brief §D | StreamOrchestrator only | PARTIAL→TODO | feature-flag bridge; full replace DEFER if deps heavy |
| Postgres runtime | brief §L | `core/db/postgres_store.py` lifecycle wired into server startup/shutdown + persist session/viewer_msg/director_decision (DATABASE_URL-gated, fire-and-forget) | DONE (2026-07-14) | — |
| Redis XADD ChatQueue | brief | in-process ChatQueue | PARTIAL | optional redis streams backend |
| LMCACHE_ENABLED | brief §E | config flag only | PARTIAL | document + compute already gates desired |
| Terraform full tree | terraform-layout | 8 modules + env | DONE | — |
| 2 public AZ | ALB/RDS | fcf7099 | DONE | — |
| CI/CD branch strategy | cicd doc | ci + deploy-dev/prod | DONE | ECS steps still stubbed (needs account) |
| Docker Hub + S3 weights | aws-arch | Dockerfiles + entrypoints | DONE | — |
| No NAT/ECR/WAF | aws-arch | enforced | DONE | — |
| AvatarForcing/EchoAvatar bench | scope | not run | DEFER | needs GPU |
| AWQ INT4 vs INT8 | scope #48 | not run | DEFER | needs GPU |
| terraform apply Seoul | ops | not run | DEFER | needs AWS account bootstrap |

## Milestone 3 scope (implement now — deploy-prep)

Offline-completable only. No real AWS apply. No GPU bench.

1. **API surface G** — sessions aliases, avatars, ws/platform, admin  
2. **Run plan + cursor + coverage** — C4/C5  
3. **Outlines Utterance schema** — C3 client-side + config  
4. **Pipecat bridge flag** — C1 skeleton (PIPECAT_ENABLED default false)  
5. **Postgres schema + optional store** — D1/D2 — **DONE (2026-07-14)**
6. **Debug gate mock routes** — B5 — **DONE (re-verified 2026-07-14)**
7. **FE LiveKit optional path** — B4 light — **DONE (re-verified 2026-07-14)**
8. **LiveKit audio publish stub** — B3 — **DONE (2026-07-14, real rtc path)**
9. **Gap report + ship checklist** for deploy test — **closed by the 2026-07-14 re-verify note above**

## Explicitly NOT in M3 (ask only if user insists now)
- Full Pipecat production cutover replacing all orchestrator paths  
- Real SFU E2E with keys  
- terraform apply  
- Model benchmarks  

## Confirmations needed from user (non-blocking for M3 code)
None for coding. Before **real deploy test** you will need:
1. AWS account + OIDC role ARNs in GitHub secrets  
2. Docker Hub login  
3. Domain on Cloudflare (or use ALB DNS)  
4. LiveKit API key/secret for real media smoke  
