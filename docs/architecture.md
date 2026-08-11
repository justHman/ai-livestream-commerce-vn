# Architecture — VN Live-Commerce Host

> Current code reference. Target decisions are in `scope-engine-and-models.md`.
> External media is complete only after its stated smoke is observed.

## Two planes

```text
CONTROL: browser -- HTTP /api/v1 + WS --> backend_service FastAPI --> renderer control API
MEDIA:   renderer -- video --> LiveKit --> browser
                          ^
                    backend PCM audio when LIVEKIT_PUBLISH=1
```

`services/product/backend_service/src/backend/` is the canonical control-plane
package for session lifecycle, Director decisions, API auth, persistence, and
control events. Renderers and
LiveKit carry media directly to the browser; frames do not transit FastAPI.
Browser responses carry only a LiveKit URL and room-join token, never server credentials.

## Current modules

```text
services/product/backend_service/src/backend/  canonical FastAPI entrypoint
services/product/llm_service/src/llm/          self-host LLM engines
services/product/tts_service/src/tts/          self-host TTS engines
services/product/avatar_service/src/avatar/    self-host avatar engines
services/platform/                             LiveKit, LMCache, Postgres, Redis runtime assets
workbench/                                     developer console (Vite/TS) for canonical /api/v1
```

The LiveAvatar cloud SDK is backend-owned under
`services/product/backend_service/src/backend/application/clients/avatar/liveavatar_sdk/`;
the canonical `backend.main` application serves `/api/v1` to the workbench
developer console.

## Lifecycle

1. `POST /sessions` or compatible `/lite/start` creates a renderer session.
2. `attach`, `plan/create`, `chat`, and `ingest` configure Director state.
3. A `StreamingAvatarBackend` runs LLM → text chunker → TTS → renderer in a
   worker thread, with a bounded video queue. Cloud backends retain `say()`.
   Text chunking is source-agnostic segmentation owned by
   `backend/application/text_chunker/`; see
   [chunking-contract.md](./chunking-contract.md) for invariants, config,
   rollback, telemetry fields, and the benchmark gate.
4. With valid LiveKit configuration, the registry creates one audio publisher
   per session, serializes PCM forwarding, and removes the entry on stop/error.
5. Session stop cancels the orchestrator, stops Director, publisher, renderer,
   and session state.
6. FastAPI shutdown repeats cleanup with a bounded timeout and closes Postgres.

The LiveKit registry is offline-covered only. It does not prove an SFU
connection, playback, browser audio, avatar video, or A/V sync.

## API surface

All routes are under `/api/v1`.

| Surface | Current behavior |
|---|---|
| `/health`, `/health/live`, `/health/ready` | public liveness and dependency readiness |
| `/sessions`, `/sessions/{id}/{say,interrupt,stop,attach,ingest,chat}` | preferred session aliases |
| `/sessions/{id}/plan/create` | deterministic run-plan creation |
| `/lite/*` | compatible legacy session paths |
| `/media/livekit/room/{id}` | room-join token when configured |
| `/avatars/*` | in-memory CRUD and idle-regenerate placeholder |
| `/ws/control/{id}`, `/ws/platform/{id}` | viewer control and platform-comment input |
| `/engines/*`, `/admin/*` | admin-authenticated engine/config/health operations |
| `/mock/*`, `/debug/*` | development/debug-gated endpoints |

Viewer routes use `BACKEND_API_TOKEN`; admin routes use `ADMIN_API_TOKEN`.
Tokens may be empty only in `APP_ENV=dev`. HTTP bodies, fields, REST rates, and
per-connection WebSocket messages are bounded in-process. Replace the limiter
with Redis before backend replicas exceed one.

## Persistence and flags

`SESSION_STORE=memory|redis` stores session metadata. `DATABASE_URL` separately
enables durable runtime Postgres; startup is bounded and readiness reports a
configured failure. Persistence logs contain operation/session context only.

```text
APP_ENV=dev|prod
RENDER_BACKEND=mock|cloud_liveavatar|remote_avatar|self_host_*
LLM_ENGINE=none|openai_compat|vllm|sglang|hf|llamacpp
TTS_ENGINE=tone|remote_http|transformers|vieneu|cosyvoice
SESSION_STORE=memory|redis
DATABASE_URL=postgresql://...
PIPECAT_ENABLED=0|1
LIVEKIT_PUBLISH=0|1
LMCACHE_ENABLED=0|1
```

Pipecat remains a bridge, not the production orchestrator. Redis Streams,
avatar video publishing, and self-host avatar implementations remain open.
