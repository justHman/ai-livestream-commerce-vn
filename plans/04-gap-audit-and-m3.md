# Gap audit — M3 primitives and M4 stabilization

> Status means offline implementation and tests, never an external deployment
> or observed media result.

## Closed offline gaps

| Area | Evidence in code |
|---|---|
| Remote engine seams | `openai_compat`, `remote_http`, `remote_avatar` adapters |
| API product surface | `/sessions/*`, `/avatars/*`, `/ws/platform/{id}`, `/admin/*` |
| Run-plan / Director | schema/endpoint, cursor/coverage, deterministic coordinator timers |
| Postgres runtime | schema, optional asyncpg store, readiness/retries, persistence logging, cleanup |
| LiveKit backend audio | room token plus `LiveKitPublisherRegistry` PCM forwarding and cleanup |
| Frontend LiveKit | `lite.html` room subscription path |
| Debug media | mock/debug routes environment-gated |
| Runtime hardening | body/field bounds, REST/WS rate limits, scoped auth, cleanup timeout |
| Infrastructure contracts | Terraform roots, image variables, Tier S profile, CI/deploy workflows |

## Open gaps

| Area | Why open |
|---|---|
| Real LiveKit E2E | No observed SFU connection and browser audio/video subscription |
| Avatar video | No SDK publisher or 75-frame LiveKit idle loop |
| Pipecat | Flag bridge does not replace the production orchestrator |
| Redis HA | No Streams, distributed locks, owner routing |
| LMCache | Desired-count flag without operational validation |
| GPU/avatar quality | No approved runtime benchmark |
| AWS/PROD | No approved apply, deployment, DNS, or release execution |

## Gate policy

The next external step is Tier S, not media or GPU. It requires a final offline
gate, explicit approval, raw ALB discovery, smoke artifacts, and teardown.
LiveKit audio/browser and avatar video require separate approval and evidence.
