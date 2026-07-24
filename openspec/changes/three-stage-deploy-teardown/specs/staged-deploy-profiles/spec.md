## ADDED Requirements

### Requirement: Three progressive deploy stages
The system MUST define exactly three progressive live deploy stages with increasing fidelity and cost. Stage N MUST NOT start until stage N-1 has recorded PASS and verified full teardown.

#### Scenario: Stage order is fixed
- **WHEN** an operator requests a live deploy
- **THEN** the allowed stages are only: Stage 1 Mock, Stage 2 Real LLM/TTS + LiveAvatar cloud, Stage 3 Real LLM/TTS + self-host avatar
- **AND** stages MUST run in that order for promotion; skipping Stage 1 for first-ever account bootstrap is forbidden

#### Scenario: Promotion requires prior stage evidence
- **WHEN** an operator attempts Stage 2 or Stage 3
- **THEN** a stage-exit report for the previous stage MUST exist under `.runtime/` with status PASS
- **AND** a teardown verification record for that previous stage MUST show zero remaining billable DEV stack resources (or an explicit temporary-stop exception approved for the same calendar day)

### Requirement: Stage 1 Mock engine profile
Stage 1 MUST use the API-only Tier S profile: no GPU capacity, zero optional service desired counts, and mock engines only.

#### Scenario: Stage 1 runtime contract
- **WHEN** Stage 1 is applied
- **THEN** runtime MUST set `RENDER_BACKEND=mock`, `LLM_ENGINE=none`, `TTS_ENGINE=tone`, `SESSION_STORE=memory`, `APP_ENV=dev`
- **AND** Terraform desired counts MUST be `desired_backend=1`, `desired_llm_tts=0`, `desired_avatar=0`, `desired_livekit=0`, `desired_lmcache=0`, `create_ec2_capacity=false`

#### Scenario: Stage 1 proves control plane only
- **WHEN** Stage 1 smoke completes
- **THEN** the stage MUST be treated as proof of Terraform, ALB, backend image, auth, session lifecycle, and logs only
- **AND** the stage MUST NOT claim LiveKit media, real LLM, real TTS, LiveAvatar, or self-host avatar success

### Requirement: Stage 2 real engines plus LiveAvatar cloud profile (no LiveKit)
Stage 2 MUST enable real LLM and real TTS self-hosted on one Spot GPU engine box, while avatar rendering uses the LiveAvatar cloud backend (`RENDER_BACKEND=cloud_liveavatar`). LiveAvatar video flows cloud → browser directly; LiveKit MUST stay off in Stage 2.

#### Scenario: Stage 2 runtime contract
- **WHEN** Stage 2 is applied (after money-safe boot, see mandatory-teardown D11)
- **THEN** runtime MUST set `RENDER_BACKEND=cloud_liveavatar`, real `LLM_ENGINE` (vLLM serving `cyankiwi/Qwen3.5-4B-AWQ-4bit`), and real `TTS_ENGINE` (vllm-omni serving VieNeu-TTS via fork `github.com/justHman/vllm-omni@feat/vieneu-tts-v0.22`)
- **AND** `LIVEAVATAR_API_KEY` (or equivalent SSM parameter) MUST be present before scale-up
- **AND** secrets MUST NOT appear in git-tracked tfvars, logs, or stage reports

#### Scenario: Stage 2 capacity defaults
- **WHEN** Stage 2 is planned
- **THEN** self-host avatar desired count MUST remain `0` (avatar is LiveAvatar cloud, no GPU avatar box)
- **AND** `desired_llm_tts=1` on a Spot `g6.xlarge` (L4 24GB) after money-safe boot, with LLM `gpu_memory_utilization=0.55` and TTS `gpu_memory_utilization=0.35` (0.10 buffer)
- **AND** `desired_livekit=0` (LiveAvatar cloud delivers video direct to browser; no SFU needed)
- **AND** `desired_lmcache=0` and `LMCACHE_ENABLED=false` (LMCache only for 2+ replicas)
- **AND** the LLM/TTS engine path MUST be identical to Stage 3 so the only Stage 2→3 variable is the avatar backend

### Requirement: Stage 3 real engines plus self-host avatar profile (LiveKit on)
Stage 3 MUST enable real LLM and real TTS with the `self_host_avatarforcing_half` avatar render backend, plus LiveKit SFU for avatar video publish to browser. Self-host backends that are not implemented MUST fail loud and MUST NOT silently fall back to mock.

#### Scenario: Stage 3 runtime contract
- **WHEN** Stage 3 is applied (after money-safe boot)
- **THEN** runtime MUST set `RENDER_BACKEND=self_host_avatarforcing_half`, real `LLM_ENGINE`, and real `TTS_ENGINE` (same engine path as Stage 2)
- **AND** GPU/avatar capacity MUST be enabled on a Spot GPU instance chosen to fit the avatar model (`g4dn.xlarge` default, `g6.xlarge` only if the model needs L4)
- **AND** `desired_avatar=1`, `create_ec2_capacity=true`, `desired_livekit=1`, `LIVEKIT_PUBLISH=1` (self-host avatar publishes video through LiveKit SFU)
- **AND** `desired_lmcache=0` (single replica)

#### Scenario: Unimplemented self-host backend fails loud
- **WHEN** Stage 3 selects a self-host backend that is not implemented
- **THEN** the backend MUST raise an explicit error (`NotImplementedError` or equivalent API 503/typed error)
- **AND** the system MUST NOT silently degrade to `mock`

### Requirement: Stage profile artifacts are explicit and reviewable
Each stage MUST have a dedicated ignored tfvars profile source (example file committed, real tfvars gitignored) and a documented engine matrix so plan review can detect wrong capacity or wrong engines before apply.

#### Scenario: Plan review catches wrong stage
- **WHEN** an operator generates a Terraform plan for a named stage
- **THEN** the plan review checklist MUST require matching render/LLM/TTS values and capacity flags for that stage
- **AND** a plan that enables GPU/avatar capacity under Stage 1 MUST be rejected

### Requirement: Spot and ARM are default compute choices
Billable compute MUST default to Spot capacity and ARM architecture where the workload supports it. On-Demand or x86 is opt-in only with a documented reason.

#### Scenario: Fargate tasks use ARM and Spot
- **WHEN** backend or LiveKit Fargate tasks are provisioned
- **THEN** the task definition architecture MUST be `arm64` and the capacity provider strategy MUST prefer `FARGATE_SPOT`
- **AND** image builds for ARM services MUST target `linux/arm64`

#### Scenario: GPU services use Spot when quota allows
- **WHEN** Stage 3 enables avatar GPU capacity
- **THEN** the ASG mixed-instances policy MUST use Spot allocation (`price-capacity-optimized`)
- **AND** the chosen GPU instance type MUST be available in the region before apply
- **AND** if Spot quota is 0 for the needed family, the apply MUST stop and surface the quota gap rather than silently fall back to On-Demand

### Requirement: Images built on GitHub Actions
Container images for every stage MUST be built and pushed by GitHub Actions workflows, not by hand on a developer machine. Images MUST be tagged by immutable SHA and architecture-matched to the consuming task.

#### Scenario: No hand-built images reach DEV
- **WHEN** a stage apply references a container image
- **THEN** the image tag MUST be a SHA produced by a GitHub Actions build job
- **AND** a `:latest` or hand-pushed tag MUST NOT be used for a billable stage

#### Scenario: Architecture match is enforced
- **WHEN** an ARM task references an image
- **THEN** the image MUST have been built with `platforms: linux/arm64`
- **AND** a GPU (x86) task MUST reference an image built with `platforms: linux/amd64`

### Requirement: Model weights sourced from S3, not HF at runtime
Runtime tasks MUST fetch model weights from the project S3 bucket via `WEIGHTS_S3_URI` + `fetch_weights.sh`. HuggingFace is the offline seeding source only; cold-start pulls from HF during a stage are forbidden because they add latency and egress cost to a billable window.

#### Scenario: Weights come from S3 at runtime
- **WHEN** a stage runs an LLM/TTS/avatar task that needs weights
- **THEN** the task MUST obtain weights via `WEIGHTS_S3_URI` (S3 sync) using the task role
- **AND** the task MUST NOT download weights from HuggingFace during the stage smoke/benchmark window

#### Scenario: HF used only for offline seeding
- **WHEN** weights are (re)seeded
- **THEN** `scripts/upload_weights_s3.py` (HF → S3) MUST run offline before any stage apply that needs those weights
- **AND** the seeding MUST NOT happen inside a billable stage window
