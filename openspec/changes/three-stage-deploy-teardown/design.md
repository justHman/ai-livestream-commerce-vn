## Context

Control plane (`core/`) and DEV Terraform are offline-ready after M4. Existing artifacts already encode the cheap baseline:

- Tier S profile: `infra/environments/dev/terraform.tier-s.tfvars.example` → mock/none/tone/memory, no EC2 capacity.
- Live smoke runbook: `docs/runbook-live-smoke-and-teardown.md` (apply gated, full destroy preferred).
- Engine seams: `RENDER_BACKEND` (`mock` | `cloud_liveavatar` | `self_host_*`), `LLM_ENGINE`, `TTS_ENGINE` in `core/config.py`.
- LiveAvatar provider: `providers/liveavatar_cloud/` with key `LIVEAVATAR_API_KEY` in gitignored `.env`.
- Self-host render: `core/render/self_host.py` must fail loud until implemented (project error-handling rule).

Gap: no single progressive ladder that sequences mock → LiveAvatar+real engines → self-host avatar, with an iron cost rule that forces teardown before fix and after report.

## Goals / Non-Goals

**Goals:**

- One operator-facing three-stage ladder with explicit engine/capacity matrices.
- Iron teardown lifecycle: fail → destroy → offline fix → redeploy → retest; pass → report → destroy.
- Stage profiles as committed `*.tfvars.example` + runbook sections; real secrets only via `TF_VAR_*` / SSM / local `.env`.
- Smoke + benchmark gates and `.runtime/stage-N-*` evidence per attempt.
- Promotion only with prior PASS + teardown verification.

**Non-Goals:**

- PROD continuous deploy or auto-apply CI to AWS.
- Implementing unfinished self-host diffusion adapters in this change if not already code-complete (Stage 3 may document fail-loud until ready).
- Business APIs (`/user/*`, `/shop/*`), NAT/private subnets, ECR, Secrets Manager, Route53, WAF.
- Leaving any DEV stack up overnight "for convenience".

## Decisions

### D1 — Three discrete profiles, not one mega-stack
Use separate tfvars examples:

| Stage | Profile file (committed example) | Engines | Capacity |
|---|---|---|---|
| 1 Mock | existing `terraform.tier-s.tfvars.example` | mock / none / tone | backend=1, GPU/optional=0 |
| 2 LiveAvatar + real engines | new `terraform.stage-2-liveavatar.tfvars.example` | cloud_liveavatar + real LLM/TTS | backend=1; llm/tts per wiring; avatar self-host=0 |
| 3 Self-host avatar | new `terraform.stage-3-selfhost.tfvars.example` | self_host_* + real LLM/TTS | enable avatar/GPU as required |

**Why not one tfvars with toggles only:** plan review must make wrong capacity obvious; named files reduce "forgot to zero GPU" mistakes.

**Alternative considered:** GitHub Environment matrix auto-deploy. Rejected — billable apply must stay human-gated.

### D2 — Real LLM/TTS self-hosted on Spot g6 (vLLM + vllm-omni fork)
Stage 2/3 LLM/TTS are self-hosted on a single Spot `g6.xlarge` (L4 24GB) EC2 task, two containers, per the existing `desired_llm_tts` + compute module wiring:

- **LLM**: vLLM serving `cyankiwi/Qwen3.5-4B-AWQ-4bit` (AWQ-INT4, qwen3_5, verified HF, ~6GB VRAM runtime per scope-engine-and-models.md). `gpu_memory_utilization=0.55` (~13GB).
- **TTS**: vllm-omni serving VieNeu-TTS via fork `github.com/justHman/vllm-omni@feat/vieneu-tts-v0.22` (verified branch HEAD `e3d48e0a`, branched from upstream tag `v0.22.0`). The fork already contains the VieNeu adapter (`vieneu.py` configs + `vieneu/pipeline.yaml`, talker stage 0 + codec stage 1, crossfade streaming). `gpu_memory_utilization=0.35` (~8GB). Buffer 0.10 (~2.5GB) for framework overhead.
- **VieNeu-TTS-v2 weights source**: local `.git` repo + HF (per user 2026-07-24, the model is held locally and seeded to S3 via `upload_weights_s3.py`; the `pnnbao-ump/VieNeu-TTS-v2` repo is NOT public on HF — do not assume HF download works at runtime, S3 is the source).

**GPU sharing 1 g6.xlarge (L4 24GB)**: vllm-omni serves one model/instance, so LLM (vLLM, port 8001) and TTS (vllm-omni, port 8002) run as two processes on the same GPU. Partition: `LLM gpu_memory_utilization=0.55` + `TTS gpu_memory_utilization=0.35` + 0.10 buffer. Total ~21GB < 24GB. Verified-fit by scope-engine-and-models.md estimates (LLM ~6GB + TTS ~1GB actual usage, partition is the ceiling). Pin fork commit `e3d48e0a` in TTS image build.

**Why self-host over remote OpenAI-compat (user decision 2026-07-24):** keeps the engine path identical across Stage 2 → Stage 3 (only the avatar renderer changes), so Stage 3 is a true ablation of the avatar backend, not a confound of engine-source change.

**Cost guard**: g6 Spot only scales up after money-safe boot (D11). Stage 2 avatar is LiveAvatar cloud (no GPU avatar box). Stage 3 adds a second Spot GPU box for avatar (`g4dn.xlarge` default) — engine g6 + avatar g4dn = two Spot boxes, both within 8 vCPU G/VT Spot quota.

**Alternative considered (rejected)**: remote OpenAI-compat LLM/TTS — would make Stage 2 vs Stage 3 differ in two variables. Rejected.

### D3 — LiveAvatar key path
Stage 2 injects `LIVEAVATAR_API_KEY` from operator environment / SSM SecureString into the backend task definition (extend secrets module wiring if missing). Never commit key. Stage reports redact it.

### D4 — Teardown-first debug loop is operational law, not optional tip
Encode in runbook + stage-verification checklist:

```text
offline gate → plan → approve → apply → smoke/bench →
  FAIL: report → destroy+verify → offline fix → (loop)
  PASS: report → destroy+verify → promote or stop
```

Temporary ECS desired-count=0 is allowed only as same-day exception with remaining billables listed; full destroy is the default exit.

### D5 — Evidence under `.runtime/`, not research notes
Each attempt writes `.runtime/stage-{1|2|3}-<timestamp>/` with smoke JSON, SUMMARY.md, teardown verification. No secrets. This matches existing Tier S log pattern.

### D6 — Stage 3 uses self_host_avatarforcing_half first; GPU instance follows the model
Stage 3 runs `self_host_avatarforcing_half` as the first self-host backend (user decision 2026-07-24). If it is not code-complete, Stage 3 fails loud + teardown per the existing error-handling rule; no silent mock fallback.

GPU instance is chosen to fit the selected avatar model while staying Spot (user decision 2026-07-24): default `g4dn.xlarge` (T4) for half-pipeline smoke; escalate to `g6.xlarge` (L4) only if the model needs L4 throughput. Both are Spot, both within the 8 vCPU G/VT Spot quota. The stage-3 tfvars documents which instance + which model + why.

### D7 — Spot + ARM default; On-Demand/x86 opt-in only
Compute defaults:

| Surface | Default | Notes |
|---|---|---|
| Backend (Fargate) | `FARGATE_SPOT` + `arm64` | already `linux/arm64` build in `deploy-dev.yml` |
| LiveKit SFU (Stage 3 only) | `FARGATE_SPOT` + `arm64` | only provisioned in Stage 3 |
| LLM+TTS engine box (Stage 2/3) | Spot `g6.xlarge` (L4 24GB), x86 | one box, two processes (LLM + TTS), GPU partitioned 0.55/0.35/0.1 |
| Avatar GPU (Stage 3) | Spot `g4dn.xlarge` (T4) default, x86 | GPU has no ARM equivalent at this tier |
| LMCache | **disabled** Stage 2/3 | `desired_lmcache=0`, `LMCACHE_ENABLED=false`; only for 2+ replicas cross-replica KV share (user decision 2026-07-24) |

**Quota check (verified 2026-07-24, `ap-northeast-2`, account 191918535424):**

────
All G and VT Spot Instance Requests ── 8 vCPU ── approved (CASE_CLOSED 2026-07-14)
Running On-Demand G and VT instances ── 16 vCPU ── approved (CASE_CLOSED 2026-07-08)
All Standard (A,C,D,H,I,M,R,T,Z) Spot ── 32 vCPU ── default
All P / DL / Inf / Trn / X Spot ── 0 ── not requested (not needed for MVP)
────

Stage 3 peak = engine g6 (4 vCPU) + avatar g4dn (4 vCPU) = 8 vCPU G/VT Spot = exactly the quota. Single-avatar smoke only; do not run 2 engine replicas + avatar simultaneously without a quota re-check. GPU AZs: `g4dn.xlarge` in 2a/2b/2c, `g6.xlarge` in 2a/2c/2d. **No new quota request needed for the planned smoke.**

**Why Spot over On-Demand:** money rule — GPU boxes only run during bounded smoke; Spot interruption is acceptable because teardown-on-interrupt is already the lifecycle.

### D8 — Images built on GitHub Actions, SHA-tagged, arch-matched
`deploy-dev.yml` already builds per-service with platform split (backend/livekit/lmcache → `linux/arm64`, llm/tts/avatar → `linux/amd64`) and tags `:dev-${{ github.sha }}`. This change makes that the **only** path: no hand `docker push`, no `:latest` on billable stages. PROD pushes six immutable SHA images but does not auto-apply (already true).

### D9 — Weights: S3 at runtime, HF only offline
Existing tooling already supports this — keep it as the rule:

- Offline seed: `scripts/upload_weights_s3.py` (HF `snapshot_download` → `aws s3 sync` to `weights/`).
- Runtime fetch: `services/scripts/fetch_weights.sh` reads `WEIGHTS_S3_URI` and `aws s3 sync` into `/models`. ECS task role already grants S3 GetObject/ListBucket.
- Storage module already has `weights/` prefix and `force_destroy` for DEV.

**Why S3 over HF-at-runtime:** HF cold pull adds latency + egress into a billable window; S3 same-region fetch is cheap, fast, cacheable, and tears down with the bucket. HF stays the offline source of truth for re-seeding only.

### D10 — Iron rule: no backup pile-up (RDS + S3)
Tighten retention so storage cost does not compound across teardown cycles:

| Resource | DEV (all stages) | PROD |
|---|---|---|
| RDS `backup_retention_days` | `0` (no automated backups) | keep minimal prod window only |
| RDS `skip_final_snapshot` | `true` | `false` (prod safety) |
| RDS manual snapshots | forbidden | explicit, time-bounded |
| S3 `enable_versioning` | `false` | `true` + immediate noncurrent expiry |
| S3 `lifecycle_noncurrent_days` | n/a (versioning off) | `1` (keep current only) |
| S3 `force_destroy` | `true` | `false` |

Current defaults need a DEV-side tightening: `backup_retention_days` defaults to `7` and DEV `main.tf` does not override it; `enable_versioning` defaults `false` (good for DEV) but PROD sets `true` without a noncurrent expiry. This change wires DEV `backup_retention_days=0`, DEV `skip_final_snapshot=true` (already), and adds a noncurrent-expiry rule for any versioned bucket.

### D11 — Iron rule: money-safe boot (desired=0 at apply, scale up after setup)
Every billable stage apply MUST start with all cost-driving desired counts / env flags OFF (`0` / `false` / `none`). Only after setup work that does not need live compute (image build, weights seed to S3, SSM secrets, config validation) is complete MAY the operator scale the cost-driving desired counts to their on-state. This prevents paying for a GPU/instance that is still loading weights, pulling an image, or waiting on config.

```text
apply with desired_*=0, engines off → verify stack healthy at zero cost
  → seed weights to S3 (offline), build+push SHA images (GitHub Actions), put SSM secrets
  → terraform apply again with desired_llm_tts=1 / desired_avatar=1 / desired_livekit=1
  → smoke/bench
  → FAIL: scale back to 0 → destroy+verify → offline fix → loop
  → PASS: report → destroy+verify
```

This is the third iron rule alongside teardown-first (D4) and no-backup-pile-up (D10).

### D12 — LiveKit only in Stage 3; Stage 2 uses LiveAvatar cloud direct-to-browser
Stage 2 avatar is LiveAvatar cloud: video flows LiveAvatar → browser directly, no LiveKit SFU needed. Stage 2 therefore sets `desired_livekit=0`. Stage 3 is the first stage that needs LiveKit (`desired_livekit=1`, `LIVEKIT_PUBLISH=1`) because the self-host avatar renderer publishes video through the SFU to the browser.

After Stage 3 bench/test PASS, the operator brings up the local frontend (`frontend/`) pointing at the Terraform-derived API origin and verifies WebRTC from API → LiveKit → browser FE on `localhost` (avatar video visible). This FE-localhost WebRTC check is a Stage 3 exit gate before teardown.

**Why LiveKit only Stage 3 (user correction 2026-07-24):** Stage 2's purpose is to prove the real LLM/TTS engine path + LiveAvatar cloud avatar; adding LiveKit there would pay for an SFU that LiveAvatar cloud does not use. Stage 3 is where self-host avatar video genuinely needs an SFU.

**Cost guard**: LiveKit is Fargate Spot arm64 (cheap), only provisioned in Stage 3. Stage 2 has no LiveKit bill.

### D13 — Stage 2 sandbox-first (free LiveAvatar avatar before credits)
LiveAvatar exposes a sandbox avatar for free ~1-min sessions (no credits): `LIVEAVATAR_SANDBOX_AVATAR_ID` default `dd73ea75-1218-4ef3-92ce-606d5f7fbc0a` (in `providers/liveavatar_cloud/sdk/client.py`, verified working 2026-06-22). Stage 2 first smoke MUST use the sandbox avatar to prove the LLM → TTS → LiveAvatar path without spending credits; only after sandbox PASS does the operator switch to a real (credit-charged) avatar for the formal benchmark. This applies the money rule to the LiveAvatar credit budget, not just AWS compute.

## Risks / Trade-offs

- **[Risk] RDS/Redis still bill while Tier S/Stage 1 runs** → Mitigation: short smoke windows; full destroy after every attempt; document residual cost even with GPU=0.
- **[Risk] LiveAvatar per-minute credits during debug** → Mitigation: teardown-first; no leave-session-open while coding; LITE preferred over FULL when architecture allows.
- **[Risk] Stage 2 remote LLM/TTS dependency on external endpoint quality** → Mitigation: record engine ids and base URLs (non-secret) in report; offline unit tests remain independent.
- **[Risk] Stage 3 GPU Spot interruption / image pull time** → Mitigation: bounded window; pre-push images offline; destroy on interrupt rather than waiting indefinitely.
- **[Risk] Operator skips destroy "just for a minute"** → Mitigation: checklist gates in runbook and tasks; no promotion without teardown verification file.
- **[Risk] RDS automated backups + S3 versioning silently accrue storage cost across teardown cycles** → Mitigation: D10 retention table; DEV `backup_retention_days=0`, versioning off, `force_destroy=true`; verify no snapshots/noncurrent versions after destroy.
- **[Risk] Spot GPU interruption mid-Stage-3 smoke** → Mitigation: bounded window; treat interrupt as teardown signal, not a retry loop; re-deploy only after offline prep.
- **[Trade-off] Human-gated apply is slower than CI auto-deploy** → Acceptable; cost control outranks deploy speed for DEV experiments.
- **[Trade-off] x86 GPU for avatar (no ARM GPU tier)** → Justified, not a violation; ARM covers backend/livekit/lmcache, GPU has no ARM equivalent at this tier.

## Migration Plan

1. Add docs + tfvars examples + checklists (no AWS changes).
2. Run offline gate only — still free.
3. Stage 1 live only after user approval of account/window/cost.
4. Destroy Stage 1 fully before any Stage 2 work.
5. Stage 2 only with LiveAvatar key present and prior Stage 1 PASS evidence.
6. Destroy Stage 2 fully before Stage 3.
7. Stage 3 only when self-host path is intentionally selected; fail loud + destroy if unimplemented.
8. Rollback for any stage = `terraform destroy` + verify; code rollback is git, offline.

## Open Questions

1. ~~Stage 2 real LLM/TTS: remote vs self-host~~ → **Resolved 2026-07-24**: self-host on Spot `g6.xlarge`, vLLM (Qwen3.5-4B-AWQ) + vllm-omni v0.22.0 fork registering VieNeu-TTS-V2.
2. ~~Stage 3 self-host enum~~ → **Resolved 2026-07-24**: `self_host_avatarforcing_half` first.
3. ~~LiveKit in Stage 2/3~~ → **Resolved 2026-07-24**: full media (`desired_livekit=1`, `LIVEKIT_PUBLISH=1`); after bench PASS, FE localhost WebRTC check (API → LiveKit → browser) is a stage-exit gate.
4. ~~Stage 3 GPU instance~~ → **Resolved 2026-07-24**: Spot, instance follows the avatar model — default `g4dn.xlarge` (T4), escalate to `g6.xlarge` (L4) only if the model needs it.
5. **New iron rule (user 2026-07-24)**: money-safe boot — apply with all cost-driving desired/env OFF, scale up only after image build + S3 weights seed + SSM secrets + config validation done. Captured as D11.
