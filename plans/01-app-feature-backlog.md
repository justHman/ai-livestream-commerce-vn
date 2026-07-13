# Plan 01 — Application migration + feature backlog

> Status: **ACTIVE backlog — wait for master roadmap approval** (`02-master-implement-roadmap.md`).  
> Parent waves: A–G in master roadmap §7.  
> Confirmed product decisions: `docs/brief-for-confirmation.md`, `docs/scope-engine-and-models.md`.

AWS resources/Docker/CI live in **Plan 00**. This plan is **code inside `core/`, `frontend/`, `services/*` app layers**.

## As-is summary

Working **monolith**:

- Director + ChatQueue + Coordinator  
- StreamOrchestrator in-process  
- LLM/TTS loaded in-process via EngineManager  
- Mock MJPEG + LiveAvatar cloud  
- Routes `/api/v1/lite/*`  
- Session store memory/redis KV only  

Target: **Backend orchestrator** calling **remote** LLM/TTS/Avatar over HTTP/SSE, **LiveKit** media, **Pipecat**, **Outlines**, **run-plan**, **Postgres runtime**.

## Waves (do in order unless noted)

### Wave A — Remote engine clients (first code)

| ID | Task | Exit |
|---|---|---|
| A1 | OpenAI-compat LLM client → `LLM_BASE_URL` SSE | stream_chunks works remote |
| A2 | Omni TTS client → `TTS_BASE_URL` | PCM/stream windows |
| A3 | Avatar HTTP client (start/stop speak) | StreamingAvatarBackend impl |
| A4 | Env: `LLM_BASE_URL`/`TTS_BASE_URL`/`AVATAR_BASE_URL`; prod default remote | config tests |
| A5 | Keep in-process engines for Colab/dev only | dual-mode documented |
| A6 | Root `pyproject.toml` + lock for backend | image buildable |

### Wave B — LiveKit media

| ID | Task | Exit |
|---|---|---|
| B1 | LiveKit room + token endpoint `/media/livekit/room/{sid}` | FE can join |
| B2 | Avatar-server publishes video + idle loop 75@25fps | no black frames |
| B3 | Backend publishes audio track (simple worker or Pipecat) | A/V sync via LiveKit |
| B4 | FE LiveKit subscribe in `lite.html` | demo path |
| B5 | MJPEG only if `DEBUG_ENABLED` | not primary |

### Wave C — Pipecat + Outlines + run-plan

| ID | Task | # |
|---|---|---|
| C1 | Pipecat replaces prod StreamOrchestrator path | #54 |
| C2 | Custom Omni TTS Pipecat service | #54 |
| C3 | Outlines Utterance schema on vLLM | #55 |
| C4 | `POST /sessions/{id}/plan/create` | #60 |
| C5 | Director cursor + BiEncoder coverage | #60 |
| C6 | Reactive > proactive tick policy | #60 |

### Wave D — Data plane

| ID | Task |
|---|---|
| D1 | Postgres runtime schema (sessions, snapshots, logs, audit) |
| D2 | asyncpg store |
| D3 | Redis ChatQueue XADD + multi-instance locks |
| D4 | WS hub remains; optional LISTEN/NOTIFY |

### Wave E — Scale flags

| ID | Task | # |
|---|---|---|
| E1 | `LMCACHE_ENABLED` wiring client + docs | #47 #56 |
| E2 | Autoscale metric names documented for TF | brief §I |

### Wave F — Avatar model benches (not MVP gate)

| ID | Task | # |
|---|---|---|
| F1 | AvatarForcing T4/L4 | #51 |
| F2 | EchoAvatar license + L4 | #52 |
| F3 | AWQ INT4 vs INT8-INT4 | #48 |
| F4 | Wire winner into avatar-server | — |

### Wave G — API surface migration

| ID | Task |
|---|---|
| G1 | `/lite/*` → `/sessions/*` (compat aliases) |
| G2 | `/avatars/*`, `/ws/platform/{sid}`, `/admin/*` |
| G3 | Never implement `/user/*` `/shop/*` (team SE) |

## Closed (do not re-open)

- Head-only MuseTalk/Ditto as commerce primary  
- llama.cpp as AWS prod LLM  
- MJPEG production media  
- NAT/ECR/Secrets Manager/Route53/WAF MVP  

## Historical

`archived/docs-historical/` and `archive/docs-historical/` hold old PLAN/TASKS/PRODUCTION — not sources of truth.

## Progress

| Wave | Status |
|---|---|
| A | not started |
| B | not started |
| C | not started |
| D | not started |
| E | not started |
| F | not started |
| G | not started |
