## Why

Backend control plane and Terraform are offline-ready (M4), but live deploy still lacks a progressive, cost-bounded path from mock API smoke to real LLM/TTS + LiveAvatar, then self-hosted avatar. Without a hard teardown-before-fix rule, every debug/retest window leaves ALB/RDS/Redis/GPU billable and idle — violating "my money is your money".

## What Changes

- Add a **three-stage live deploy ladder** with explicit engine profiles:
  1. **Stage 1 — Mock**: `RENDER_BACKEND=mock`, `LLM_ENGINE=none`, `TTS_ENGINE=tone`, no GPU/optional services (existing Tier S baseline).
  2. **Stage 2 — Real engines + LiveAvatar cloud**: real LLM + real TTS, avatar via `cloud_liveavatar` (LiveAvatar API key from `.env`/SSM).
  3. **Stage 3 — Real engines + self-host avatar**: real LLM + real TTS, avatar via self-host renderer (`self_host_*`), GPU capacity enabled.
- Codify an **iron teardown discipline** for every stage:
  - On any failure/bug: **full teardown first** → fix offline → re-deploy → re-test/benchmark.
  - On success: **report then full teardown** before leaving the session.
  - Forbidden: idle billable infra while coding, debugging, or after smoke ends.
- Codify a second **iron storage-retention discipline** (no backup pile-up):
  - RDS automated backups: keep only the latest retention window needed for the env (DEV: `0` preferred; never multi-week pile).
  - Skip final snapshots on DEV destroy; no manual snapshot hoarding.
  - S3: versioning off for DEV weights/assets; if versioning is on anywhere, expire noncurrent versions immediately / keep current object only.
  - Forbidden: accumulating old DB snapshots or S3 object versions “just in case”.
- Prefer **Spot** capacity and **ARM** where the platform allows; build images on **GitHub Actions**; seed model weights to **S3** (HF is offline source, not runtime cold-start).
- Add stage-specific tfvars profiles, runbooks, smoke/benchmark gates, and a stage-exit report template under `.runtime/`.
- Wire stage promotion only after prior stage PASS + teardown verified (no stack left running).

## Capabilities

### New Capabilities

- `staged-deploy-profiles`: Defines the three progressive deploy stages (engine matrix, capacity flags, secrets, image contracts) and how DEV Terraform/runtime env switches between them.
- `mandatory-teardown`: Cost-control lifecycle: always destroy (or fully stop-and-verify) billable infrastructure before debug/fix and after each stage success/report; no idle window; no multi-generation RDS/S3 backup retention.
- `stage-verification`: Per-stage smoke + benchmark acceptance criteria, log capture, pass/fail gates, and promotion rules between stages.

### Modified Capabilities

- *(none — no existing `openspec/specs/` baseline yet)*

## Impact

- **Docs/runbooks**: extend `docs/runbook-live-smoke-and-teardown.md`, `docs/runbook-deploy-prep.md`; add stage-2/3 profiles and teardown checklist.
- **Infra**: new/extended DEV tfvars examples for stage-2 (LiveAvatar + real engines) and stage-3 (self-host avatar + GPU); Spot/ARM capacity defaults; RDS `backup_retention_days` + S3 versioning tightened to “latest only”.
- **Runtime config**: `RENDER_BACKEND` / `LLM_ENGINE` / `TTS_ENGINE` / base URLs / LiveAvatar secrets via SSM/`TF_VAR_*` (no secrets in git). Weights via `WEIGHTS_S3_URI` + existing `fetch_weights.sh` / `upload_weights_s3.py`.
- **CI/workflows**: image builds on GitHub Actions (already platform-split arm64/amd64); no automatic unpaid live apply; offline pytest remains free.
- **Ops cost surface**: ALB, RDS, Redis always billable in DEV apply; stage-2/3 add engine/GPU/LiveAvatar API usage — bounded windows only. Spot quota in `ap-northeast-2` already raised (G/VT Spot = 8 vCPU).
- **Out of scope**: PROD release automation, business `/user/*` `/shop/*`, NAT/private subnet, ECR, Secrets Manager, Route53, WAF (MVP exclusions remain).
