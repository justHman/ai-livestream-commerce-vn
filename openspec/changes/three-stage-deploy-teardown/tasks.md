## 1. Offline foundations (no AWS bill)

- [x] 1.1 Document the three-stage engine/capacity matrix in `docs/runbook-deploy-prep.md` (Stage 1 mock, Stage 2 LiveAvatar+real engines, Stage 3 self-host avatar) and link the iron teardown rule
- [x] 1.2 Extend `docs/runbook-live-smoke-and-teardown.md` with the mandatory loop: offline gate → plan → approve → apply → smoke/bench → FAIL destroy+fix offline / PASS report → destroy+verify → promote
- [x] 1.3 Add stage-exit report template (engine matrix, timestamps, smoke/bench results, teardown verification, no secrets) under `docs/` or `.runtime/README` pointer
- [x] 1.4 Confirm offline gate still green: `uv lock --check`, `uv run pytest core/tests/ -q`, ruff scope from deploy-prep, `terraform fmt -check` + validate global/dev/prod

## 2. Stage profiles (tfvars + secrets wiring)

- [x] 2.1 Keep Stage 1 profile as `infra/environments/dev/terraform.tier-s.tfvars.example` (mock/none/tone, capacity zeros); ensure runbook names it Stage 1
- [x] 2.2 Add `infra/environments/dev/terraform.stage-2-liveavatar.tfvars.example` with `render_backend=cloud_liveavatar`, `llm_engine=vllm` (Qwen3.5-4B-AWQ), `tts_engine=vllm-omni` (fork feat/vieneu-tts-v0.22), `desired_llm_tts=1` (Spot g6.xlarge, LLM gpu_mem_util=0.55, TTS=0.35, buffer 0.10), `desired_livekit=0`, `desired_avatar=0`, `desired_lmcache=0`/`LMCACHE_ENABLED=false`
- [x] 2.3 Add `infra/environments/dev/terraform.stage-3-selfhost.tfvars.example` with `render_backend=self_host_avatarforcing_half`, same LLM/TTS as Stage 2, `desired_avatar=1`, `create_ec2_capacity=true`, Spot GPU (`g4dn.xlarge` default), `desired_livekit=1`/`LIVEKIT_PUBLISH=1`, `desired_lmcache=0`
- [x] 2.4 Wire or document `LIVEAVATAR_API_KEY` (and LLM/TTS secrets if needed) via SSM/`TF_VAR_*` into backend task env for Stage 2; never commit secrets
- [x] 2.5 Plan-review checklist: reject Stage 1 plans with GPU/optional desired>0; reject Stage 2 plans with `desired_livekit>0`; reject Stage 3 plans missing real engines/LiveKit or leaking secrets; reject On-Demand GPU when Spot quota allows; reject first-apply plans with cost-driving desired>0 (money-safe boot)
- [x] 2.6 Confirm Fargate backend capacity provider strategy prefers `FARGATE_SPOT` and tasks stay `arm64`; confirm Stage 2/3 engine+avatar ASG uses Spot `price-capacity-optimized`
- [x] 2.7 Confirm GPU AZ availability for chosen Stage 2/3 instance types (`g6.xlarge` 2a/2c/2d, `g4dn.xlarge` 2a/2b/2c) before any plan; stay within 8 vCPU G/VT Spot quota (Stage 3 peak = g6 engine 4vCPU + g4dn avatar 4vCPU = 8vCPU = quota ceiling)
- [x] 2.8 Pin vllm-omni fork `github.com/justHman/vllm-omni@feat/vieneu-tts-v0.22` (HEAD `e3d48e0a`) in the TTS image build; do not pull upstream unstable; the fork already contains the VieNeu adapter
- [ ] 2.9 Seed VieNeu-TTS-v2 weights to S3 via `upload_weights_s3.py` (HF `pnnbao-ump/VieNeu-TTS-v2` verified public 2026-07-24, 13 files; S3 is the runtime source, never HF cold pull during a billable window)

## 3. Stage verification tooling

- [x] 3.1 Codify Stage 1 smoke commands (health/live, ready, engines, session lifecycle) writing to `.runtime/stage-1-<ts>/`
- [x] 3.2 Codify Stage 2 smoke: real engines (vLLM + vllm-omni VieNeu) visible, one LLM→TTS→LiveAvatar cloud avatar path, `desired_livekit=0` verified, redacted logs, latency sample (no LiveKit in Stage 2); sandbox-first — use `LIVEAVATAR_SANDBOX_AVATAR_ID` (`dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`, free ~1-min, no credits) for first smoke, switch to real avatar only for formal bench
- [x] 3.3 Codify Stage 3 smoke: `self_host_avatarforcing_half` start/speak/avatar-video-publish-through-LiveKit/stop or explicit fail-loud; no silent mock fallback; bench timings; after bench PASS, FE localhost WebRTC check with avatar video visible
- [x] 3.4 Add teardown verification script/checklist (ECS, RDS, ElastiCache, ALB, unexpected EC2/NAT, **leftover RDS snapshots, S3 noncurrent versions**) writing `teardown-verify.md` into the stage log dir
- [x] 3.5 Ensure `scripts/bench_api.py` (or stage smoke timings) can record a bounded Stage 2/3 sample without requiring infra left running after report
- [x] 3.6 Verify images referenced by any stage are SHA-tagged from GitHub Actions (no `:latest`, no hand-pushed); ARM tasks ↔ arm64 images, GPU tasks ↔ amd64 images

## 3a. Money-safe boot procedure (iron rule)

- [x] 3a.1 Document two-phase apply per stage: Phase 0 zero-cost apply (all cost-driving desired=0, engines off) → verify stack healthy → Phase 1 scale-up apply (desired_llm_tts/avatar/livekit on)
- [x] 3a.2 Phase 0→1 gate: setup complete checklist (GitHub Actions SHA images pushed, S3 weights seeded, SSM secrets present, config validated) before Phase 1
- [x] 3a.3 On any stage FAIL: scale cost-driving desired back to 0 (or full destroy) before offline fix; re-attempt repeats Phase 0→1

## 3b. Iron storage-retention wiring (no backup pile-up)

- [x] 3b.1 Set DEV RDS `backup_retention_days = 0` (override module default 7) in `infra/environments/dev/main.tf` `module "database"` block
- [x] 3b.2 Confirm DEV `skip_final_snapshot = true` and `deletion_protection = false` already set; document "no manual RDS snapshots for DEV stages" in runbook
- [x] 3b.3 Confirm DEV `module "storage"` keeps `enable_versioning = false` (default) and `force_destroy = true`; ensure stage-2/3 tfvars do not flip versioning on
- [x] 3b.4 For any versioned bucket (PROD), set `lifecycle_noncurrent_days = 1` so only current object version survives; add the lifecycle rule if missing
- [x] 3b.5 Add teardown-verify step that lists `aws rds describe-db-snapshots` + `aws s3api list-object-versions` and fails if any leftover after destroy

## 3c. Weights pipeline (S3 runtime, HF offline)

- [x] 3c.1 Confirm `WEIGHTS_S3_URI` is wired into Stage 3 (avatar) task env via compute module; document the value pattern in stage-3 tfvars example
- [ ] 3c.2 Run `scripts/upload_weights_s3.py` offline to seed avatar weights to S3 before any Stage 3 apply (do NOT pull HF during a billable window)
- [x] 3c.3 Confirm `services/scripts/fetch_weights.sh` is the runtime entrypoint and task role has S3 GetObject/ListBucket on the weights prefix

## 4. Live Stage 1 — Mock (billable; human-gated)

- [x] 4.1 User confirms AWS account, time window, estimated residual cost (ALB/RDS/Redis), and full-destroy teardown route
- [x] 4.2 Offline gate green → copy Tier S example to ignored tfvars → `terraform plan` → human approve → apply (no auto-approve default)
- [x] 4.3 Run Stage 1 smoke; write SUMMARY; on FAIL: destroy+verify → offline fix → only then re-apply (loop)
- [x] 4.4 On PASS: write stage-exit report → full destroy+verify → stop or promote; never leave stack idle

## 5. Live Stage 2 — Real LLM/TTS (self-host g6) + LiveAvatar cloud, no LiveKit (billable; human-gated)

- [ ] 5.1 Require Stage 1 PASS report + teardown verification before any Stage 2 plan
- [ ] 5.2 Confirm `LIVEAVATAR_API_KEY` present (env/SSM); confirm vllm-omni fork `e3d48e0a` pinned in TTS image; confirm Qwen3.5-4B-AWQ + VieNeu-TTS-v2 weights seeded to S3 (HF public source); default `LIVEAVATAR_SANDBOX_AVATAR_ID` for first smoke
- [ ] 5.3 Money-safe boot: Phase 0 apply (cost-driving desired=0, engines off) → verify healthy → build/push SHA images via GitHub Actions → seed weights → put SSM secrets
- [ ] 5.4 User confirms cost window (AWS g6 Spot + LiveAvatar credits) → Phase 1 apply (`desired_llm_tts=1` on Spot g6, `desired_livekit=0`, `desired_avatar=0`, `desired_lmcache=0`, `render_backend=cloud_liveavatar`, LLM gpu_mem_util=0.55, TTS=0.35)
- [ ] 5.5 Run Stage 2 smoke/bench (LLM→TTS→LiveAvatar cloud avatar, video direct to browser, no LiveKit); on FAIL: scale-to-0/destroy+verify → offline fix → redo Phase 0→1; on PASS: report → destroy+verify

## 6. Live Stage 3 — Real LLM/TTS + self-host avatar (`avatarforcing_half`) + LiveKit full media (billable; human-gated)

- [ ] 6.1 Require Stage 2 PASS report + teardown verification before Stage 3 plan
- [ ] 6.2 Confirm `self_host_avatarforcing_half` is code-complete; if not, plan to fail loud + teardown (do not mark PASS); document chosen GPU instance + avatar model in stage-3 profile
- [ ] 6.3 Money-safe boot: Phase 0 apply (cost-driving desired=0) → verify healthy → confirm SHA images + S3 weights + SSM secrets
- [ ] 6.4 User confirms GPU/Spot cost window → Phase 1 apply (`desired_avatar=1`, `desired_llm_tts=1`, `desired_livekit=1`, `LIVEKIT_PUBLISH=1`, `create_ec2_capacity=true`, Spot GPU g4dn engine g6, `desired_lmcache=0`)
- [ ] 6.5 Run Stage 3 smoke/bench (avatar video publish through LiveKit SFU); after bench PASS run FE localhost WebRTC check (avatar video visible); on FAIL: scale-to-0/destroy+verify → offline fix → redo Phase 0→1; on PASS: report → destroy+verify

## 7. Closeout

- [ ] 7.1 Final AWS check: no leftover DEV ECS tasks, ALB, RDS, Redis, EC2 from these stages (or explicit user-approved retain list)
- [ ] 7.2 Archive stage reports under `.runtime/` (gitignored) and summarize outcomes in a short handoff note
- [ ] 7.3 Update agent memory with stage results, cost lessons, and any profile defaults that changed

## 8. References (skills to invoke during apply)

- [ ] 8.1 When wiring Stage 2 LiveAvatar integration: invoke `liveavatar-integrate` skill — assess current stack, pick Embed vs FULL vs LITE mode, guide implementation end-to-end
- [ ] 8.2 When Stage 2 LiveAvatar integration breaks (silent avatar, garbled audio, session start fail, missing events, API errors): invoke `liveavatar-debug` skill for systematic troubleshooting
- [ ] 8.3 Both skills are available via the Skill tool; pull them at the apply step that touches `providers/liveavatar_cloud/` or Stage 2 smoke, not before
