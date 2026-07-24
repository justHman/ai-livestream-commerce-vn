# Architecture — VN Live-Commerce Host

> Current code reference. Target decisions are in `scope-engine-and-models.md`.
> External media is complete only after its stated smoke is observed.

## Two planes

```text
CONTROL: browser -- HTTP /api/v1 + WS --> core FastAPI --> renderer control API
MEDIA:   renderer -- video --> LiveKit --> browser
                          ^
                    backend PCM audio when LIVEKIT_PUBLISH=1
```

`core/` owns session lifecycle, Director decisions, API auth, persistence, and
control events. Renderers and LiveKit carry media directly to the browser;
frames do not transit FastAPI. Browser responses carry only a LiveKit URL and
room-join token, never server credentials.

## Current modules

```text
core/
├── api/                auth, limits, v1 routes and WebSocket hubs
├── db/                 optional Postgres runtime store and schema
├── director/           FSM, clustering, run-plan cursor, coordinator
├── llm/                local and openai_compat engine adapters
├── render/             cloud, mock, remote avatar, self-host seam, orchestrator
├── stream/             text chunker
├── tts/                tone, local, and remote_http adapters
├── livekit_publish.py  per-session backend PCM publisher registry
├── livekit_tokens.py   room-join token minting
├── server.py           app factory, readiness, lifespan cleanup
└── store.py            memory/Redis session metadata store
```

`providers/liveavatar_cloud/` is an independent provider SDK. Its standalone
Colab service keeps a smaller `/api` contract; `frontend/lite.html` targets
`core.server` and appends `/api/v1` to the origin.

## Lifecycle

1. `POST /sessions` or compatible `/lite/start` creates a renderer session.
2. `attach`, `plan/create`, `chat`, and `ingest` configure Director state.
3. A `StreamingAvatarBackend` runs LLM → text chunker → TTS → renderer in a
   worker thread, with a bounded video queue. Cloud backends retain `say()`.
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
