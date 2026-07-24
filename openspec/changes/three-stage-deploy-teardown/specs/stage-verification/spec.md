## ADDED Requirements

### Requirement: Offline gate before every live stage
Before any stage apply, the operator MUST run the offline validation gate (lock check, unit tests, ruff, terraform fmt/validate). Live apply is forbidden while the offline gate is red.

#### Scenario: Offline gate blocks live apply
- **WHEN** offline pytest, ruff, or terraform validate fails
- **THEN** Stage N apply MUST NOT proceed
- **AND** the failure MUST be fixed offline without creating billable infrastructure

### Requirement: Stage 1 smoke acceptance
Stage 1 MUST prove control-plane health and session lifecycle against the Terraform-derived ALB origin.

#### Scenario: Stage 1 pass criteria
- **WHEN** Stage 1 smoke runs
- **THEN** it MUST succeed on `/api/v1/health/live`, `/api/v1/health/ready`, authenticated `/api/v1/engines`, and a full session start → attach → plan → chat → stop cycle
- **AND** backend request logs MUST be captured under `.runtime/stage-1-<timestamp>/`
- **AND** the smoke base URL MUST come from Terraform outputs, not a remembered DNS name

### Requirement: Stage 2 smoke and benchmark acceptance (LiveAvatar cloud, no LiveKit)
Stage 2 MUST prove real LLM/TTS wiring and LiveAvatar cloud avatar session (video direct to browser). No LiveKit dependency. Stage 2 MUST use the LiveAvatar sandbox avatar first (free, ~1-min sessions, no credits) before spending real credits.

#### Scenario: Stage 2 sandbox-first smoke
- **WHEN** Stage 2 first smoke runs
- **THEN** it MUST use the LiveAvatar sandbox avatar (`LIVEAVATAR_SANDBOX_AVATAR_ID` default `dd73ea75-1218-4ef3-92ce-606d5f7fbc0a`) to prove the LLM → TTS → LiveAvatar path without spending credits
- **AND** only after sandbox smoke PASS MAY the operator switch to a real (credit-charged) avatar for the formal benchmark

#### Scenario: Stage 2 pass criteria
- **WHEN** Stage 2 smoke runs (sandbox or real)
- **THEN** engines endpoint MUST report the configured real LLM (vLLM Qwen3.5-4B-AWQ) and TTS (vllm-omni VieNeu-TTS)
- **AND** a session MUST complete at least one real speak path that exercises LLM → TTS → LiveAvatar cloud avatar
- **AND** LiveAvatar API key MUST never appear in captured logs or reports
- **AND** `desired_livekit` MUST remain `0` (no LiveKit bill in Stage 2)
- **AND** a bounded latency/throughput sample MUST be recorded in the stage report

### Requirement: Stage 3 smoke and benchmark acceptance (LiveKit full media)
Stage 3 MUST prove `self_host_avatarforcing_half` avatar backend start/stop with real LLM/TTS and full LiveKit media (audio + avatar video through SFU), or fail loud if the backend is unimplemented.

#### Scenario: Stage 3 pass criteria when backend is implemented
- **WHEN** Stage 3 uses an implemented `self_host_avatarforcing_half` backend
- **THEN** session start MUST attach the self-host renderer and complete at least one speak path with real LLM/TTS
- **AND** avatar video MUST publish through LiveKit (`desired_livekit=1`, `LIVEKIT_PUBLISH=1`)
- **AND** stop/cleanup MUST release avatar/GPU resources
- **AND** benchmark timings MUST be recorded in the stage report
- **AND** after bench PASS, a frontend localhost WebRTC check (API → LiveKit → browser FE, avatar video visible) MUST succeed before teardown

#### Scenario: Stage 3 fails closed when backend is unimplemented
- **WHEN** Stage 3 selects an unimplemented self-host backend
- **THEN** the stage MUST FAIL with an explicit error
- **AND** the operator MUST tear down immediately and MUST NOT mark the stage PASS

### Requirement: Stage-exit report format
Every stage attempt (PASS or FAIL) MUST produce a stage-exit report under `.runtime/` containing stage id, timestamps, engine matrix, smoke/benchmark results, cost-window notes, teardown action, and teardown verification.

#### Scenario: Report written before next action
- **WHEN** a stage smoke/benchmark finishes
- **THEN** a `SUMMARY.md` (or equivalent) MUST be written before promotion or session end
- **AND** the report MUST exclude secrets and Authorization values
- **AND** FAIL reports MUST still include teardown verification after destroy

### Requirement: Re-test loop is teardown-gated
Re-test and re-benchmark after a failure MUST only run on a freshly deployed stack after offline fix; they MUST NOT run against a stack left up "to save time".

#### Scenario: Re-test after fix
- **WHEN** Stage N previously failed and an offline fix is ready
- **THEN** the operator MUST confirm prior teardown verification exists
- **AND** only then MAY Stage N re-apply, re-smoke, and re-benchmark
- **AND** success still requires report + final teardown
