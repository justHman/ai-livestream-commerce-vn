# Plan 01 — application migration backlog

> Code advanced through migration primitives. This record identifies offline
> implementation boundaries; it does not claim external media or production
> operations completed.

## Current baseline

- Director, deterministic timers, run-plan cursor/coverage, and compatible
  `/lite/*` plus preferred `/sessions/*` routes are implemented.
- `openai_compat`, `remote_http`, and remote-avatar seams coexist with local
  and development engines.
- Postgres runtime persistence is optional through `DATABASE_URL`; session KV
  remains separately memory/Redis.
- Input, body, REST, and WebSocket limits are bounded per process.

## Migration status

| Wave | State | Remaining exit |
|---|---|---|
| A — remote engines | primitives implemented | integration smoke with deployed engines |
| B — LiveKit | token mint, frontend subscribe path, PCM registry, debug-gated mock routes | real SFU audio/browser test; avatar video publisher/idle loop |
| C — Pipecat/Outlines/run-plan | run-plan/cursor/coverage and schema hooks; Pipecat bridge is flag-only | production Pipecat cutover and vLLM guided-decoding config |
| D — data plane | Postgres schema/store/lifecycle | Redis Streams, ownership, multi-instance coordination |
| E — scale | Terraform desired-count flags | autoscaling and LMCache operational validation |
| F — avatar benchmarks | deferred | approved benchmark and winner integration |
| G — API surface | sessions, avatars, platform WS, admin surface | remove compatibility only after clients migrate |

## Invariants

- Never implement `/user/*` or `/shop/*`.
- MJPEG is a development/debug path, not production media.
- LiveKit publisher code needs a publisher/SFU/browser observation before a
  media claim.
- Pipecat, Redis Streams, self-host avatar, and GPU work remain separate from
  API-only Tier S.

## Next order

1. Approved LiveKit audio/browser smoke.
2. Avatar video publisher and idle-frame media test.
3. Remote engine integration smoke.
4. Pipecat production cutover and Redis multi-instance work.
5. Cost-bounded GPU/avatar benchmarks.
