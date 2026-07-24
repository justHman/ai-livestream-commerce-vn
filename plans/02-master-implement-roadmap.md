# Master implementation roadmap — M4 status

> Active-state record after M4 stabilization. It supersedes prior as-is claims
> that infrastructure, API surfaces, persistence, and LiveKit primitives were
> missing.

## Verified offline baseline

| Surface | State |
|---|---|
| FastAPI control plane | Session aliases, avatars, platform/control WS, admin, auth, bounded requests and WebSockets |
| Director | Timer invariants, run-plan cursor/coverage, graceful stop-all |
| Engines/render | Local and remote seams; mock path for offline/Tier S |
| Persistence | Optional Postgres store with bounded startup/readiness/shutdown |
| LiveKit | Room token, frontend subscription path, per-session PCM publisher registry |
| Infrastructure | Terraform global/DEV/PROD roots, Tier S profile, Docker contracts, CI/CD workflows |
| Provider/Colab | Current provider path and API-key naming; notebook/runbook preflight alignment |

## Still incomplete or unverified

| Surface | Required evidence |
|---|---|
| AWS Tier S | Approved apply, API smoke, log capture, teardown |
| LiveKit media | Publisher connection, SFU acceptance, browser audio; then avatar video/idle frames |
| Pipecat | Replace production `StreamOrchestrator`; current flag is a bridge |
| Guided output | vLLM deployed with verified Outlines guided-decoding configuration |
| Redis HA | Streams, owner routing, locks, multi-instance behavior |
| GPU/model services | Cost-bounded engine/avatar benchmarks and health smoke |
| PROD release | Manual workflow, rollback exercise, post-deploy smoke |

## Execution order

1. Run final offline gate and record evidence.
2. Obtain explicit approval for Tier S only; apply, smoke, capture, teardown.
3. Obtain separate approval for LiveKit audio/browser, then avatar video.
4. Integrate remote services, Pipecat, and Redis HA after service-contract tests.
5. Treat GPU scale-up and PROD release as separate costed operations.

## Locked MVP exclusions

NAT, private subnets, ECR, Route53, AWS WAF, Secrets Manager, API Gateway,
weights in images, llama.cpp as AWS production LLM, head-only avatar media,
`/user/*`, and `/shop/*` remain out of scope.
