# Ship checklist — M4 offline readiness

> Scope: code and infrastructure contracts only. No AWS apply, image publish,
> DNS change, release, or real LiveKit operation is implied by this checklist.

## Implemented and offline-covered

| Area | Current state |
|---|---|
| API and sessions | `/sessions/*`, `/avatars/*`, `/ws/platform/{id}`, `/admin/*`, auth, and bounded input/WS handling |
| Director | Timer accounting, run-plan cursor/coverage, and coordinator shutdown |
| Postgres runtime | Optional `DATABASE_URL` store with bounded retries, readiness, persistence, and shutdown cleanup |
| LiveKit audio | Room-token minting plus per-session PCM publisher registry; cleanup on session stop and app shutdown |
| Frontend media | `lite.html` can subscribe when supplied a LiveKit URL and token |
| Terraform | Global, DEV, and PROD roots plus Tier S example validate with backend disabled |
| CI/CD | Production-source Ruff, offline tests, Terraform checks, backend build, DEV/PROD workflow contracts |
| Colab/provider | Current provider paths and `LIVEAVATAR_API_KEY` contract documented and checked offline |

## Deliberately unverified

- Real LiveKit SFU publishing and browser audio subscription in one room.
- Avatar video publishing and the 75-frame idle loop.
- Pipecat replacing `StreamOrchestrator`.
- Redis Streams/ownership for multi-replica backends.
- GPU LLM/TTS, LMCache, or avatar-model benchmarks.
- Terraform apply, Docker Hub publication, AWS OIDC deployment, PROD release,
  DNS, or a public endpoint.

## Offline gate before a live-operation request

```powershell
uv lock --check
uvx ruff check core/api core/db core/debug core/director core/llm core/render core/schemas core/stream core/tts core/config.py core/engine_manager.py core/livekit_publish.py core/livekit_tokens.py core/pipecat_bridge.py core/server.py core/store.py providers scripts/bench_api.py scripts/upload_weights_s3.py
uv run pytest core/tests/ -q
terraform fmt -check -recursive infra
terraform -chdir=infra/environments/global init -backend=false
terraform -chdir=infra/environments/global validate
terraform -chdir=infra/environments/dev init -backend=false
terraform -chdir=infra/environments/dev validate
terraform -chdir=infra/environments/prod init -backend=false
terraform -chdir=infra/environments/prod validate
git diff --check
```

## First external operation

Use [runbook-live-smoke-and-teardown.md](./runbook-live-smoke-and-teardown.md)
after explicit confirmation of resources, time window, cost, smoke, and
teardown. Tier S keeps `LIVEKIT_PUBLISH=0` and all GPU/media services at zero.

Before claiming media readiness, observe publisher connection, PCM accepted by
the SFU, and browser audio subscription in a separately approved smoke.
