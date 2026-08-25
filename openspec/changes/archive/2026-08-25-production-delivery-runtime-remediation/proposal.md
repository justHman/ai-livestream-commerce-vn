## Why

The independent full-codebase source audit at exact repository SHA `6ff71f39f6139d31206893a38523e1d8d59e8191` found a set of cross-cutting packaging, runtime-composition, delivery, security, distributed-state, CI, and developer-console defects that can make an otherwise accepted feature fail or report a false-success state after packaging/deployment.

This remediation is deliberately **not** a product-semantic redesign. Change A `adaptive-speech-text-chunking` and Change B `approved-script-authoring-pipeline` remain CLOSED/FROZEN. The objective is to make the already-accepted system truthful and executable at its packaging/runtime/delivery boundaries while preserving the owner-approved provider-first strategy:

- LLM: third-party/BYOK/custom provider by default; self-host is optional/future.
- TTS: VieNeu-TTS Python SDK is the current target; first initialization may download its model through the SDK/provider path.
- Avatar: third-party provider first; self-host Avatar is optional/future.
- LiveKit: managed LiveKit Cloud is acceptable/preferred initially; self-host remains optional.
- AWS is one deployment implementation, not the product architecture.

The current active remediation must not revive stale Stage 2/3 deployment work, trigger billable GPU/cloud validation, or spend implementation time making dormant self-host paths production-ready.

## What Changes

- Package all backend runtime resources required by accepted Change B behavior into the actual backend image and verify them from the built container.
- Separate liveness from readiness and make every canonical readiness endpoint return HTTP `503` when required runtime state is unavailable and `200` only when ready.
- Make provider/deployment selection and backend runtime composition use one coherent contract; keep the backend lightweight and provider/transport-neutral.
- Ensure hosted/provider selection forces unused self-host LLM/TTS/Avatar capacity to zero and prevents a stub from being interpreted as a real self-host production service.
- Correct the current VieNeu deployment path so the SDK/provider download path is not forced through S3, AWS CLI, or offline mode by default.
- Keep generic self-host model-weight bootstrap dormant; do not make it a current release dependency.
- Introduce provider-neutral durable storage seams for voice metadata/assets; AWS-specific libraries such as `boto3` are adapter dependencies only.
- Enforce fail-closed protected API authentication and reject placeholder/development credentials in real production mode.
- Fix GitHub Actions reusable-workflow invocation, authenticated `gh api` evidence collection, OIDC environment identity, cross-job evidence transfer, dependency graph validity, service identity, candidate migration identity, and final-gate result aggregation.
- Add built-container smoke tests that catch packaging/entrypoint/readiness defects without downloading real models, requiring GPU, or mutating cloud infrastructure.
- Define multi-replica request limiting as a shared logical quota plus per-replica local overload protection, with authenticated identity preferred over IP.
- Preserve distributed-state correctness/security when managed Redis/Postgres are enabled, including TLS/auth requirements and stale-owner fencing behavior.
- Realign/rebuild Workbench as a developer test console against the current backend contract; do not treat it as a production frontend.
- Allow local/offline Terraform validation without requiring live S3 remote state; keep remote state as an explicit deployment concern.
- Mark Stage 2/3 as PAUSED/STALE/DO NOT EXECUTE for this remediation.
- Preserve cloud portability by keeping application-core interfaces independent of AWS-specific storage/compute concepts.

## Capabilities

### New Capabilities

- `production-delivery-runtime-remediation`: truthful production artifact/runtime boundaries, provider-first composition, delivery-chain correctness, portable storage/state seams, distributed request protection, built-container CI verification, and a current-backend Workbench test console.

### Modified Capabilities

- *(none; this change repairs cross-cutting delivery/runtime behavior without reopening the accepted product semantics of archived Change A/B capabilities)*

## Dependency and Sequencing

This change starts from audited `main@6ff71f39f6139d31206893a38523e1d8d59e8191` and the V3 audit handoff identified below.

Change A and Change B are upstream accepted behavior and MUST remain frozen. Implementers may fix packaging, runtime wiring, deployment, CI, auth, state, and test-console defects that prevent accepted behavior from working, but MUST NOT redesign accepted authoring/chunking semantics.

Implementation uses a **parallel cluster orchestration model** rather than serial cluster execution:

```text
Coordinator
├── Orchestrator Cluster 0 ──> Implementer(s), one active at a time
├── Orchestrator Cluster A ──> Implementer(s), one active at a time
├── Orchestrator Cluster B ──> Implementer(s), one active at a time
├── Orchestrator Cluster C ──> Implementer(s), one active at a time
├── Orchestrator Cluster D ──> Implementer(s), one active at a time
└── Orchestrator Cluster E ──> Implementer(s), one active at a time
```

All six cluster orchestrators MAY run concurrently in isolated worktrees/branches created from the same coordinator-recorded base SHA.

Agent depth is capped at exactly two delegation edges:

```text
Coordinator -> Orchestrator -> Implementer
```

- An Orchestrator MUST NOT spawn another orchestrator, reviewer, coordinator, or any subagent other than its own implementer.
- An Implementer MUST NOT spawn any subagent.
- Within one cluster, implementation remains serialized: one active implementer at a time.
- The Orchestrator itself performs the cluster/task review after an implementer returns. A failed review triggers another implementer/fix round; PASS returns a cluster result package to the Coordinator.
- Only the Coordinator integrates cluster branches and performs the final integrated exact-head verification/review.

For every cluster, the Orchestrator applies Superpowers `systematic-debugging` to establish/reproduce root cause and requires implementers to use `test-driven-development` for repairs. Do not combine unrelated clusters into a broad refactor.

To reduce merge conflicts, file/domain ownership is enforced:

- Cluster 0 owns backend artifact packaging and readiness behavior/tests.
- Cluster A owns GitHub Actions/delivery-chain wiring and CI workflow fanout.
- Cluster B owns provider/runtime composition, VieNeu configuration, provider/self-host capacity invariants, and credential/provider readiness.
- Cluster C owns distributed state, portable voice/object persistence, rate limiting, Redis/Postgres security, and fencing.
- Cluster D owns Workbench.
- Cluster E owns local/offline Terraform-state validation paths plus documentation/example truthfulness.

If an Orchestrator discovers a required change in another cluster's owned domain, it SHALL return an `INTEGRATION_DEPENDENCY` instead of editing outside its ownership. The Coordinator resolves that dependency during integration or re-dispatches the owning Orchestrator.

## Evidence Source

Authoritative remediation handoff:

`AI_LIVESTREAM_FULL_CODEBASE_AUDIT_REMEDIATION_HANDOFF_V3_2026-08-24.md`

SHA-256:

`455110becb298795ed6d14345326ff6d852f2cdb713bdf370bfb1c7cf4ffb884`

The packaged copy under `supporting/` is evidence/supporting context. If this OpenSpec artifact and the handoff disagree, owner-approved decisions in the handoff V3 and current source at the exact repair head take precedence.

## Impact

- **Backend image/runtime**: resource packaging, readiness semantics, provider-client composition, auth fail-closed behavior, multi-replica quota interface.
- **TTS**: current VieNeu model-source configuration and portable voice/object storage seams.
- **Avatar**: provider-first truthfulness guard; no implementation of real self-host Avatar in this change.
- **LLM**: provider-first/BYOK composition; no local GPU engine in backend and no requirement to activate self-host bootstrap.
- **CI/CD**: GitHub Actions workflow correctness, evidence transfer/authentication, candidate identity, final-gate propagation, built-container smokes.
- **Terraform**: truthfulness/invariants and offline/local-state-compatible validation; no live apply required.
- **Data plane**: shared rate-limit store interface, Redis/Postgres security contracts, lease/fencing regression coverage when enabled.
- **Workbench**: stale console replaced/realigned to current backend contracts for development testing only.
- **Documentation/OpenSpec**: stale Stage 2/3 explicitly paused, dormant self-host capabilities not represented as ready.

## Out of Scope

- Reopening Change A or Change B accepted semantics.
- Real self-host Avatar implementation.
- Self-host LLM rollout or optimization.
- Generic model-weight bootstrap activation.
- LiveKit self-host deployment.
- Stage 2 or Stage 3 execution/reconciliation.
- GPU tests or real model downloads.
- Live AWS/GCP/Azure mutation by default.
- Implementing every cloud provider.
- Replacing provider APIs with gRPC/WS solely on theoretical performance grounds.
- Production-hardening Workbench as an end-user frontend.
