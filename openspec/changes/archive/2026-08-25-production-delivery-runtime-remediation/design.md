# Production Delivery Runtime Remediation — Design

## Context

The audit at `6ff71f39f6139d31206893a38523e1d8d59e8191` found two classes of risk:

1. **Active correctness defects**: the final artifact/runtime/delivery path can be broken or falsely green even though accepted source-level product behavior is correct.
2. **Dormant/future self-host work**: code exists for future model hosting, but the owner intentionally uses providers first to minimize early cost and operational burden.

This design prevents implementers from confusing the second class with the first.

## Goals

- Make packaged artifacts contain what accepted runtime code needs.
- Make health/readiness signals truthful.
- Align deployment/provider configuration with the clients the backend actually instantiates.
- Keep the backend lightweight and provider/transport-neutral.
- Make current VieNeu initialization follow the SDK/provider path without mandatory AWS coupling.
- Make CI catch artifact/startup/readiness failures cheaply.
- Make authentication, delivery evidence, shared quotas, and distributed state safe for multi-replica operation.
- Rebuild Workbench as a current-backend test console.
- Preserve cloud portability.
- Keep Stage 2/3 and billable self-host work paused.

## Non-goals

- Product-semantic redesign of Change A/B.
- Self-host Avatar/LLM implementation.
- GPU/live-cloud validation.
- Generic weight downloader activation.
- All-cloud Terraform abstraction.
- Production end-user frontend work.

---

## Decision 1 — Frozen product semantics, repair outer boundaries only

Change A/B behavior is immutable for this change. A defect that prevents accepted code from being packaged, composed, authenticated, reached, or correctly observed may be repaired. A change that alters accepted authoring/chunking semantics is out of scope.

A repair must be classified before implementation as one of:

- packaging/artifact;
- readiness/health;
- provider/runtime composition;
- delivery/CI;
- security/auth;
- distributed state/rate limit;
- developer test console;
- configuration/documentation truthfulness.

Anything else requires a separate proposal.

## Decision 2 — Backend runtime resources are part of the production artifact

The backend image SHALL contain the service-level runtime resource tree needed by Change B, including the project-owned skill and curated safety/profanity resources.

Preferred immediate implementation: copy the resource tree into the image at the same stable path the existing loaders expect. A later move to package-data/`importlib.resources` is acceptable only if it preserves immutable project-owned resources and does not broaden this remediation.

Built-image verification SHALL run `SkillLoader().content()`, `content_hash()`, and representative resource loads from inside the image.

## Decision 3 — Liveness and readiness are distinct contracts

- **Liveness** means the process is alive.
- **Readiness** means the application can safely accept the governed traffic for that service.

Canonical readiness handlers SHALL return:
- `200` only when ready;
- `503` when required runtime state/dependencies are unavailable.

Load-balancer/promotion checks may only use readiness after these HTTP semantics are correct. A JSON body saying `not_ready` with HTTP `200` is invalid.

## Decision 4 — Provider-first composition with transport-neutral application interfaces

The backend remains lightweight. It SHALL NOT start local vLLM/GPU inference to make environment variables align.

Application-facing contracts should remain provider/transport-neutral, e.g.:

- LLM: `generate`, `stream`;
- TTS: synthesize/stream contract;
- Avatar: provider/session/render contract.

Adapters may use provider HTTP/SSE, gRPC streaming, WebSocket, or another measured transport. SSE is HTTP streaming; Protobuf is an encoding. A transport rewrite requires measured evidence and a separate need, not this remediation.

Provider selection SHALL deterministically choose the corresponding concrete client and SHALL not silently fall back to echo/stub/tone behavior in real production readiness.

## Decision 5 — Provider mode and self-host capacity must agree

Current default strategy:
- LLM provider/BYOK first;
- VieNeu SDK target for TTS;
- Avatar provider first.

If an external/provider mode is selected, unused self-host desired capacity SHALL be zero.

If real self-host Avatar is selected while only the stub exists, configuration/plan/startup SHALL fail clearly. The stub may remain for explicit test mode but SHALL never produce a production-ready self-host signal.

## Decision 6 — VieNeu owns its current model bootstrap

The current VieNeu target uses its Python SDK/provider model initialization. The deployment SHALL NOT force `WEIGHTS_S3_URI`, AWS CLI, or Hugging Face/Transformers offline mode by default for the VieNeu engine.

Model source is engine-specific. A future self-host engine may use an object-store/cache bootstrap, but that future path is dormant.

Generic `fetch_weights.sh` is therefore not a current release dependency. If touched, changes must be static/minimal and may not trigger real weight downloads/GPU work.

## Decision 7 — Storage is provider-neutral

Voice persistence separates:

- **metadata**: IDs, ownership, provider, version, status, hashes/URIs, timestamps — durable metadata store;
- **binary/reference assets**: audio/reference files/model artifacts — `ObjectStore`-style interface.

Core TTS/application code SHALL not require S3 or `boto3`. Optional implementations may target S3, GCS, Azure Blob, R2, MinIO, or local development storage. Local filesystem persistence is explicit dev/test only.

Do not implement every adapter in this change. Implement the interface and the adapters required by current tests/runtime, while keeping AWS dependencies optional.

## Decision 8 — Multi-replica rate limiting has two layers

1. **Shared logical quota** across all backend replicas using a shared `RateLimitStore`.
2. **Local overload/concurrency protection** per replica.

Logical quotas prefer authenticated `user_id`/account/API-key/tenant identity. IP is fallback only. Proxy-derived client IP requires an explicit trusted-proxy policy.

Development may use an in-memory store. Real multi-replica production uses a shared store.

## Decision 9 — Distributed state must remain safe when enabled

When Redis/Postgres production stores are enabled:
- Redis connection security requirements are explicit (TLS/auth as applicable).
- Postgres TLS policy is explicit.
- session/work locks protect against stale owners via lease/fencing semantics; a stale worker cannot write after ownership has moved.

This change preserves accepted Change B lease/fencing semantics and adds/repairs enforcement/tests rather than redesigning them.

## Decision 10 — Protected authentication fails closed

If protected-route configuration/container resolution fails, access is denied. Health endpoints may remain public according to existing intent.

Placeholder/development fixture secrets SHALL be rejected in real production mode. Credential naming for provider/internal service clients and servers must be coherent where those services are enabled.

## Decision 11 — Delivery evidence is explicit data, not runner-local accident

GitHub Actions SHALL satisfy all of the following:

- reusable workflows invoked at the job level according to GitHub syntax;
- `gh api` calls explicitly authenticated;
- OIDC subject/environment names exactly match configured environments for enabled cloud deployment;
- files/results passed between jobs via declared artifacts/outputs;
- every `needs.X` reference is a direct declared dependency;
- service IDs are canonical and mapped once;
- migration uses the exact candidate backend image identity;
- final `gate` fails if any governed validation fails, including repo-tools.

No live cloud mutation is required to validate workflow structure.

## Decision 12 — Built-container smoke is a first-class CI gate

Ordinary CI runners SHALL build and boot production-shaped containers for cheap checks without:
- GPU;
- large model downloads;
- live cloud infrastructure.

At minimum the backend smoke proves packaged resources, startup, liveness/readiness behavior, and intended generation preflight seam.

Model-service smokes exercise import/config/entrypoint/health seams with mocks/fixtures and no real model load.

Affected-area fanout must ensure a shared file change reruns every relevant service check.

## Decision 13 — Workbench is a developer test console

Workbench is not a production frontend.

The existing stale Workbench should be realigned/rebuilt to:
- use the current backend request/response schema;
- preserve viewer/admin token ownership;
- handle SSE authentication, `MessageEvent.data`, reconnect, and event identity correctly;
- preserve version/gate/approval identifiers;
- preferably generate client/types from backend OpenAPI to reduce drift;
- pass a real local-backend smoke.

No SEO, production deployment, or end-user hardening belongs in this change.

## Decision 14 — Terraform local development is not coupled to paid remote state

Repository cloud deployments may use remote state when deployment resumes, but local/offline development and validation SHALL work without requiring a live S3 backend.

Do not delete real cloud resources automatically. Cost cleanup requires a separate live inventory and explicit approval.

## Decision 15 — Cloud portability is a core boundary

AWS-specific infrastructure remains an implementation under `infra/`, not a requirement of application-core contracts.

Application/core abstractions must not introduce new mandatory:
- S3;
- IAM;
- ECS;
- AWS SDK
concepts.

This change does not implement GCP/Azure. It only preserves portable seams.

## Decision 16 — Stage 2/3 and self-host expansion are paused

For this change:

```text
LIVE CLOUD MUTATION          = NONE by default
GPU TEST                     = NONE
REAL MODEL DOWNLOAD TEST     = NONE
STAGE 2                      = PAUSED / STALE / DO NOT EXECUTE
STAGE 3                      = PAUSED / STALE / DO NOT EXECUTE
SELF-HOST LLM/AVATAR ROLLOUT = DEFERRED
```

Historical Stage 2/3 artifacts are retained as evidence only.

---

## Implementation Clusters

### Cluster 0 — artifact + readiness truthfulness

Scope:
- backend resources in image;
- readiness HTTP 200/503 semantics and readiness-facing checks.

Exit gate:
- production-shaped backend container smoke is green;
- readiness cannot be falsely green through HTTP 200 on a not-ready body.

### Cluster A — delivery trust chain

Scope:
- GitHub workflow syntax;
- `gh api` auth;
- OIDC environment identities;
- evidence transport;
- dependency graph;
- service IDs/digests;
- migration candidate identity;
- final gate aggregation.

Exit gate:
- actionlint/static workflow tests green;
- no live cloud apply required.

### Cluster B — provider/runtime composition + VieNeu

Scope:
- deployment/runtime vocabulary;
- concrete provider client selection;
- no local GPU engine in backend;
- provider mode zeroes unused self-host capacity;
- self-host Avatar selection fails clearly while only a stub exists;
- current VieNeu SDK model-source path;
- auth/credential parity;
- real-production secret/readiness behavior;
- immutable image identity where real production deploy is enabled.

Exit gate:
- provider-shaped tests prove intended concrete clients;
- VieNeu path is not blocked by S3/offline/AWS CLI.

### Cluster C — distributed state, storage abstraction, rate limiting

Scope:
- provider-neutral object store/voice persistence;
- shared `RateLimitStore`;
- local overload protection;
- identity/proxy policy;
- Redis/Postgres security;
- lock fencing regression.

Exit gate:
- two-replica logical quota test;
- persistence/restart test;
- stale-owner test;
- non-S3 path runs without `boto3`.

### Cluster D — Workbench

Scope:
- current backend client/types;
- auth ownership;
- SSE payload/reconnect;
- version/gate/approval identity;
- local backend smoke.

Exit gate:
- typecheck/lint/build/tests and real-local-backend smoke green.

### Cluster E — configuration/documentation truthfulness

Scope:
- stale local config vocabulary;
- Stage 2/3 paused markers;
- dormant self-host markers;
- local-state-compatible Terraform validation docs/config.

Exit gate:
- no active documentation instructs implementers to run stale Stage 2/3 or mandatory current S3 weight bootstrap.

---

## Parallel Coordinator / Orchestrator SDD Protocol

### Agent topology and hard depth limit

```text
Coordinator
├── Cluster-0 Orchestrator
│   └── Implementer
├── Cluster-A Orchestrator
│   └── Implementer
├── Cluster-B Orchestrator
│   └── Implementer
├── Cluster-C Orchestrator
│   └── Implementer
├── Cluster-D Orchestrator
│   └── Implementer
└── Cluster-E Orchestrator
    └── Implementer
```

Maximum delegation depth is **2**:

```text
Coordinator -> Orchestrator -> Implementer
```

No additional nested agent is permitted.

- Coordinator does not implement cluster code.
- Orchestrator does not spawn reviewers or nested orchestrators.
- Implementer does not spawn subagents.
- Each Orchestrator may dispatch/resume/fresh-dispatch implementers sequentially, but MUST have at most one active implementer for its cluster at a time.
- All six Orchestrators may run in parallel because they operate in isolated worktrees/branches.

### Coordinator responsibilities

Before dispatch:
1. Record exact base SHA.
2. Create/verify six isolated cluster worktrees/branches from that same SHA.
3. Publish cluster ownership boundaries and immutable global constraints.
4. Start a coordinator ledger containing cluster branch/worktree, base SHA, status, integration dependencies, and returned head SHA.

During execution:
1. Dispatch all six cluster Orchestrators concurrently.
2. Do not edit cluster implementation directly.
3. Receive only structured cluster result packages.
4. Route cross-cluster needs to the owning cluster; do not let workers bypass ownership.
5. Keep Stage 2/3 and billable work paused.

Integration:
1. Integrate only clusters with `PASS`.
2. Default integration order after parallel implementation is `0 -> A -> B -> C -> D -> E`; this is merge/integration ordering only, not implementation ordering.
3. Resolve merge conflicts by preserving the owning cluster's interface contract. For non-trivial conflicts, re-dispatch the affected owning Orchestrator against the integration head rather than hand-editing silently.
4. Run fresh integrated full verification.
5. Perform the final broad source review at the exact integrated head.

### Orchestrator responsibilities

Each cluster Orchestrator is an SDD controller scoped to one cluster.

1. Read only its cluster brief, global constraints, OpenSpec requirement pointers, and relevant V3 handoff sections.
2. Run `superpowers:systematic-debugging` reasoning/reproduction before assigning a repair.
3. Dispatch one Implementer for one reviewable task/fix at a time.
4. Require RED evidence before the minimal repair and GREEN evidence after it.
5. Review the Implementer's diff/report itself for:
   - spec compliance;
   - cluster ownership;
   - test evidence;
   - no frozen Change A/B semantic drift;
   - no billable/deferred scope activation;
   - code quality/regression risk.
6. On FAIL, dispatch/resume an Implementer with the exact findings; maximum 5 fix rounds for a task.
7. On PASS, append cluster ledger evidence and continue to the next task.
8. When all cluster tasks pass, return a structured `CLUSTER_PASS` package to the Coordinator.

The Orchestrator MUST NOT delegate review to another subagent because that would exceed the user-approved orchestration design and add unnecessary nested agents.

### Implementer responsibilities

An Implementer:
- receives one focused brief;
- MUST NOT spawn any subagent;
- writes/runs the RED test;
- makes the minimal TDD repair;
- runs focused GREEN/regression tests;
- commits its work;
- writes a concise report for its Orchestrator.

### Cross-cluster ownership / conflict protocol

Cluster ownership:

| Cluster | Primary ownership |
|---|---|
| 0 | backend Docker/runtime resources, canonical health/readiness handlers/tests, readiness-facing checks |
| A | `.github/workflows`, delivery evidence plumbing, actionlint/fanout CI wiring |
| B | runtime/provider config, LLM/TTS/Avatar provider composition, VieNeu model-source config, provider/self-host capacity, provider credentials/readiness |
| C | rate-limit store/local overload, voice/object persistence abstraction, Redis/Postgres production security, lease/fencing |
| D | Workbench only |
| E | local/offline Terraform state-validation mechanism, docs/examples/stale Stage 2/3 markers |

A cluster MUST NOT casually edit another cluster's owned domain.

If a cross-cluster change is required, the Orchestrator returns:

```text
INTEGRATION_DEPENDENCY
owner_cluster: <0|A|B|C|D|E>
required_interface/change: <precise statement>
reason: <why local cluster cannot complete correctly without it>
blocking: <yes|no>
```

The Coordinator either:
- routes it to the owning Orchestrator while parallel work continues; or
- defers it to integration when non-blocking.

### Cluster result contract

Every Orchestrator returns exactly one final status:

```text
CLUSTER_PASS
CLUSTER_BLOCKED
```

`CLUSTER_PASS` includes:
- cluster ID;
- branch/worktree;
- base SHA;
- head SHA;
- commit list;
- files changed;
- RED/GREEN commands and results;
- regression commands and results;
- review/fix-round ledger;
- rulings;
- integration dependencies;
- confirmation: no nested agents beyond Implementer;
- confirmation: no Stage 2/3, GPU, real model download, or live cloud mutation;
- confirmation: Change A/B semantics preserved.

### Final integrated gate

A cluster PASS is necessary but not sufficient for completion.

Only the Coordinator may declare the change complete after:
- all required clusters PASS;
- integration conflicts/dependencies are resolved;
- fresh exact-integrated-head suites pass;
- OpenSpec validation passes;
- built-container smokes pass;
- final broad source review is clean or all residual findings are explicitly ruled/deferred according to the plan.


## Risks and Controls

- **Risk: fixing future self-host code instead of active provider paths.**
  Control: deferred table and Stage 2/3 no-run gate.
- **Risk: hiding a missing dependency with fallback/stub behavior.**
  Control: readiness/fail-loud scenarios.
- **Risk: broad multi-cloud rewrite.**
  Control: portable interfaces only; no all-cloud implementation.
- **Risk: CI becomes expensive.**
  Control: ordinary runners, no GPU/model download/live cloud.
- **Risk: Workbench scope balloons.**
  Control: developer-test-console acceptance only.
- **Risk: cluster work reopens Change B.**
  Control: frozen semantics requirement plus independent review.

## Final Verification

The repair head must provide fresh evidence for all applicable suites:

- backend unit/integration/contract/coverage;
- provider/TTS tests without real model download;
- repo-tools;
- Workbench test/typecheck/lint/build;
- ruff;
- actionlint;
- Terraform fmt/validate/tests in local-state-compatible mode;
- OpenSpec validation;
- built-container smokes;
- `git diff --check`.

Historical pass counts are not accepted as fresh evidence.
