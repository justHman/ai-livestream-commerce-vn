# Ship checklist — deploy-prep ready (code complete offline)

> Branch: `feature/implement-aws-mvp`  
> Verified offline: **261 passed, 2 skipped** (2026-07-11)  
> Runbook: [runbook-deploy-prep.md](./runbook-deploy-prep.md)  
> Gap audit: [../plans/04-gap-audit-and-m3.md](../plans/04-gap-audit-and-m3.md)

## Code vs confirmed docs

| Doc | Requirement | Status |
|---|---|---|
| aws-architecture | Public subnet only, S3 GW EP, no NAT | **DONE** (`infra/modules/network` — 2 public AZs) |
| aws-architecture | ECS + Fargate Spot + GPU ASG + LMCache ASG | **DONE** skeleton (`compute`) |
| aws-architecture | RDS + Redis, not public | **DONE** (`database`) |
| aws-architecture | ALB origin, Cloudflare Free (ops) | **DONE** ALB module; CF is external |
| aws-architecture | Docker Hub public + S3 weights | **DONE** Dockerfiles + entrypoints |
| aws-architecture | SSM secrets | **DONE** secrets module |
| terraform-layout | Root & child modules tree | **DONE** |
| cicd-branch-strategy | ci + deploy-dev + deploy-prod OIDC | **DONE** (ECS update steps stubbed `if: false`) |
| brief §D HTTP/SSE engines | remote LLM/TTS clients | **DONE** |
| brief §D Pipecat | full replace orchestrator | **STUB** `PIPECAT_ENABLED` / `pipecat_bridge` — full cutover **DEFER** |
| brief §D Outlines | guided JSON Utterance | **PARTIAL** client schema + body hook; needs vLLM `--guided-decoding-backend outlines` at serve |
| brief §D run-plan | plan/create + cursor + coverage | **DONE** offline deterministic plan + cursor/coverage helpers |
| brief §E LiveKit | token + publish tracks | **PARTIAL** token mint **DONE**; publish **STUB**; real SFU smoke **DEFER** |
| brief §M API | sessions/avatars/ws/platform/admin | **DONE** (+ `/lite/*` compat) |
| brief §L Postgres runtime | schema + store | **DONE** schema + optional asyncpg store (not auto-wired lifecycle) |
| scope-engine | LLM AWQ / Omni TTS / half-full avatar | **CONFIG/DOCS**; model benches **DEFER** (GPU) |
| architecture.md | control plane module map | **MOSTLY** — refresh routes list in follow-up doc PR |

## What you can do next (deploy test)

1. Bootstrap AWS OIDC + tfstate (`infra/environments/global`) — **manual**  
2. `terraform plan` in `infra/environments/dev`  
3. Set GitHub secrets: `AWS_ROLE_ARN_DEV`, Docker Hub  
4. Flip deploy-dev ECS steps from `if: false` → true when cluster exists  
5. Push images; smoke: health → sessions start → LiveKit token → chat  

## Explicitly NOT required before first infra smoke

- Pipecat production cutover  
- AvatarForcing / EchoAvatar / AWQ benches  
- LiveKit real join with paid keys  
- Multi-AZ RDS (still single-AZ MVP compute pin)  

## Confirm with user before real money apply

1. AWS account ID + whether OIDC deploy roles already exist  
2. Docker Hub namespace OK as `justhman/*`  
3. Domain on Cloudflare or temporary ALB DNS  
4. Budget alarm email for SNS  

## Branch tip commits (M3)

- `d137543` utterance + guided json  
- `5b7a2c9` run plan + cursor coverage  
- `912c66c` sessions/avatars/platform/admin  
- `24aad7a` deploy-prep runbook  
- `260a1b2` FE LiveKit path  
- `f71e5b3` pipecat + livekit publish stubs  
- `a81127d` postgres schema + store  
