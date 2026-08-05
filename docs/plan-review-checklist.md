# Plan-review checklist (per stage)

> Run this checklist over every `terraform plan` before human approval. Reject
> the plan on any failed item. Ties to the three-stage ladder in
> [runbook-deploy-prep.md](./runbook-deploy-prep.md) and the iron rules in
> [runbook-live-smoke-and-teardown.md](./runbook-live-smoke-and-teardown.md).

## All stages

- [ ] Offline gate green (`uv lock --check`, `uv run pytest tests/ci/ -q`, ruff scope, `terraform fmt -check` + validate global/dev/prod) — live apply forbidden while red.
- [ ] No `-auto-approve` on apply or destroy.
- [ ] Images are SHA-tagged from GitHub Actions (no `:latest`, no hand-pushed tags) on billable stages.
- [ ] ARM tasks ↔ `linux/arm64` images; GPU tasks ↔ `linux/amd64` images.
- [ ] Backend / LiveKit Fargate tasks use `arm64` + capacity provider strategy prefers `FARGATE_SPOT`.
- [ ] GPU ASG (engine/avatar) uses Spot `price-capacity-optimized`; `spot_capacity_percentage=100` unless Spot quota gap forces On-Demand (then document why).
- [ ] GPU instance types available in the chosen AZs before apply (`g6.xlarge` 2a/2c/2d, `g4dn.xlarge` 2a/2b/2c).
- [ ] Stage N-1 PASS report + teardown verification exist under `.runtime/` before Stage N (N≥2) plan.
- [ ] Prior stage teardown-verify.md shows zero remaining billable DEV stack resources (no leftover RDS snapshots, no S3 noncurrent versions).
- [ ] DEV RDS `backup_retention_days=0`, `skip_final_snapshot=true`, `deletion_protection=false`.
- [ ] DEV S3 `enable_versioning=false`, `force_destroy=true`; PROD S3 `lifecycle_noncurrent_days=1`.
- [ ] No secrets in tfvars, plan output, or logs (`LIVEAVATAR_API_KEY`, db password, API tokens come via SSM/`TF_VAR_*`).

## Stage 1 — Mock

- [ ] `render_backend=mock`, `llm_engine=none`, `tts_engine=tone`, `session_store=memory`, `app_env=dev`.
- [ ] `desired_backend=1`, all other desired=0, `create_ec2_capacity=false`.
- [ ] REJECT if any GPU/optional desired>0, or `create_ec2_capacity=true`.

## Stage 2 — LiveAvatar cloud + real engines (no LiveKit)

- [ ] `render_backend=cloud_liveavatar`, `llm_engine=vllm`, `tts_engine=vieneu`.
- [ ] `desired_llm/desired_tts=1` (Spot g6.xlarge); `desired_avatar=0`; `desired_livekit=0`; `desired_lmcache=0`/`LMCACHE_ENABLED=false`.
- [ ] Money-safe boot: first-apply plan shows `desired_llm/desired_tts=0` (Phase 0). REJECT a first-apply plan with cost-driving desired>0.
- [ ] REJECT if `desired_livekit>0` (LiveKit is Stage 3 only).
- [ ] `LIVEAVATAR_API_KEY` present in SSM (`liveavatar/api_key`) before Phase 1 scale-up.
- [ ] Sandbox-first: first smoke uses `LIVEAVATAR_SANDBOX_AVATAR_ID` (`dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`); real avatar only for formal bench.
- [ ] TTS image build pins vllm-omni fork commit `e3d48e0a` (branch `feat/vieneu-tts-v0.22`).
- [ ] Qwen3.5-4B-AWQ + VieNeu-TTS weights seeded to S3 offline (VieNeu from local `.git`, not HF cold pull).

## Stage 3 — self-host avatar + LiveKit full media

- [ ] `render_backend=self_host_avatarforcing_half`, same LLM/TTS as Stage 2 (`llm_engine=vllm`, `tts_engine=vieneu`).
- [ ] `desired_avatar=1`, `desired_llm/desired_tts=1`, `desired_livekit=1`, `LIVEKIT_PUBLISH=1`, `desired_lmcache=0`, `create_ec2_capacity=true`.
- [ ] Money-safe boot: first-apply plan shows all cost-driving desired=0 (Phase 0). REJECT a first-apply plan with cost-driving desired>0.
- [ ] Avatar GPU Spot `g4dn.xlarge` default (escalate to `g6.xlarge` only if the model needs L4 — document why).
- [ ] Spot quota check: g6 engine 4 vCPU + g4dn avatar 4 vCPU = 8 vCPU G/VT Spot = quota ceiling. Single-avatar smoke only. REJECT 2 engine replicas + avatar simultaneously without a quota re-check.
- [ ] `self_host_avatarforcing_half` code-complete; if not, plan MUST fail loud + teardown (no silent mock fallback).
- [ ] LiveKit API key/secret in SSM (`livekit/api_key`, `livekit/api_secret`) before Phase 1 scale-up.
- [ ] Avatar weights seeded to S3 offline; `WEIGHTS_S3_URI` wired from storage output.
- [ ] FE localhost WebRTC check scheduled after bench PASS (API → LiveKit SFU → browser FE, avatar video visible).
