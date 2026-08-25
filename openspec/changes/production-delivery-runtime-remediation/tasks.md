# Production Delivery Runtime Remediation — Implementation Tasks

> **Execution contract:** Run clusters in parallel through the strict hierarchy `Coordinator -> Orchestrator -> Implementer`. Each cluster Orchestrator uses `superpowers:systematic-debugging`, dispatches at most one active Implementer at a time, requires TDD RED/GREEN evidence, and performs its own review/fix loop. No nested orchestrator/reviewer is allowed and Implementers cannot spawn subagents. Keep Change A/B product semantics frozen. Do not execute Stage 2/3, GPU tests, real model downloads, or live cloud mutations.

**Audit base:** `6ff71f39f6139d31206893a38523e1d8d59e8191`  
**Authoritative handoff:** `AI_LIVESTREAM_FULL_CODEBASE_AUDIT_REMEDIATION_HANDOFF_V3_2026-08-24.md` (`sha256:455110becb298795ed6d14345326ff6d852f2cdb713bdf370bfb1c7cf4ffb884`)

## Coordinator parallel-dispatch gates

- [x] P0 Record one exact common base SHA for all six cluster branches. (`879e601` develop tip, owner-directed; tree identical to audited main `6ff71f3`)
- [x] P1 Create/verify six isolated worktrees/branches: `cluster-0`, `cluster-a`, `cluster-b`, `cluster-c`, `cluster-d`, `cluster-e`.
- [x] P2 Verify every worktree is clean and starts at the recorded common base.
- [x] P3 Create the Coordinator ledger with cluster worktree, branch, base SHA, status, returned head SHA, integration dependencies, and rulings. (`.runtime/coordinator-ledger-pdr.md`)
- [x] P4 Publish file/domain ownership exactly as specified in `design.md`; Orchestrators must return `INTEGRATION_DEPENDENCY` for out-of-owner changes.
- [x] P5 Dispatch Cluster 0/A/B/C/D/E Orchestrators concurrently.
- [ ] P6 Enforce max hierarchy depth: Coordinator -> Orchestrator -> Implementer. No Orchestrator may spawn a reviewer/nested orchestrator; no Implementer may spawn any subagent.
- [ ] P7 Each Orchestrator may have only one active Implementer at a time inside its own cluster.
- [ ] P8 Coordinator does not hand-edit cluster implementation while orchestrators are active.
- [ ] P9 Accept a cluster only with `CLUSTER_PASS` package containing exact head, commits, files, RED/GREEN/regression evidence, self-review/fix ledger, integration dependencies, and scope confirmations.
- [ ] P10 After all non-blocked cluster results return, integrate PASS branches. Default merge/cherry-pick order is `0 -> A -> B -> C -> D -> E`; this ordering applies only to integration.
- [ ] P11 For non-trivial integration conflicts, re-dispatch the owning Orchestrator against the integration head; do not silently invent a coordinator fix.
- [ ] P12 Run final verification only on the integrated exact head.

### Orchestrator review/fix-loop contract

For every reviewable task inside its cluster:

- [ ] ORCH.1 Orchestrator establishes/reproduces root cause before dispatching repair.
- [ ] ORCH.2 Orchestrator dispatches one focused Implementer brief.
- [ ] ORCH.3 Implementer produces RED -> minimal repair -> GREEN/regression -> commit -> report.
- [ ] ORCH.4 Orchestrator reviews the diff/report itself for spec compliance, quality, ownership, frozen semantics, and non-billable scope.
- [ ] ORCH.5 If review FAILS, dispatch/resume an Implementer with exact findings, then self-review again.
- [ ] ORCH.6 Maximum 5 fix rounds per task. A still-load-bearing failure after the cap makes the cluster `CLUSTER_BLOCKED`; do not hide it.
- [ ] ORCH.7 If review PASSES, ledger the task and proceed.
- [ ] ORCH.8 When every cluster task passes, return `CLUSTER_PASS` to Coordinator.

## Global gates

- [ ] G0.1 Create an isolated repair branch/worktree from the intended repair base and record base SHA.
- [ ] G0.2 Read `proposal.md`, `design.md`, this `tasks.md`, `specs/production-delivery-runtime-remediation/spec.md`, and the packaged V3 handoff before editing code.
- [ ] G0.3 Record all currently active findings as RED reproductions/tests where practical; do not use historical CI as fresh evidence.
- [ ] G0.4 Freeze Change A/B semantics: no redesign of accepted TextChunker/finality/deadline or approved-script generation/gate/approval behavior.
- [ ] G0.5 Mark Stage 2/3, self-host Avatar rollout, self-host LLM rollout, and generic weight-bootstrap activation as non-executable for this change.
- [ ] G0.6 Keep all verification non-billable by default: no live cloud apply/destroy, no GPU, no real model download.

---

## Cluster 0 — Artifact and readiness truthfulness

### 0.1 Backend runtime resources

- [x] 0.1.1 Use systematic debugging to reproduce missing `backend_service/resources/` inside the production-shaped backend image.
- [x] 0.1.2 Add a RED built-image test proving `SkillLoader().content()`/`content_hash()` and curated profanity/safety loads fail in the current image.
- [x] 0.1.3 Make the minimal Docker/package-resource repair; do not fetch mutable resources remotely at runtime.
- [x] 0.1.4 Run the focused GREEN built-image test.
- [x] 0.1.5 Add/retain one no-cloud/no-GPU authoring preflight that reaches the intended generation seam.
- [x] 0.1.6 Commit as an independently reviewable artifact-packaging change.

### 0.2 Readiness HTTP semantics

- [x] 0.2.1 Reproduce every canonical readiness route that returns `not_ready` with HTTP 200.
- [x] 0.2.2 Add RED tests for required-dependency failure -> HTTP 503 and ready -> HTTP 200.
- [x] 0.2.3 Repair backend/LLM/TTS canonical readiness handlers without turning liveness into readiness.
- [x] 0.2.4 Only after 503 semantics are correct, update load-balancer/promotion checks that must use readiness.
- [x] 0.2.5 Run focused health/readiness regressions and commit.

### Cluster 0 exit gate

- [x] 0.G Backend built-container smoke is green.
- [x] 0.G Readiness 200/503 tests are green.

---

## Cluster A — Delivery trust chain

### A.1 Reusable workflows and static validation

- [x] A.1.1 Reproduce each reusable workflow incorrectly invoked under `steps[*].uses`.
- [x] A.1.2 Add/enable `actionlint` (or equivalent) RED coverage that catches the invalid structure.
- [x] A.1.3 Move reusable workflow calls to valid job-level `uses` while preserving intended inputs/secrets.
- [x] A.1.4 Run actionlint/static workflow tests and commit.

### A.2 GitHub API evidence authentication

- [x] A.2.1 Enumerate governed `gh api` calls and reproduce missing-token behavior.
- [x] A.2.2 Add RED checks requiring explicit token environment wiring.
- [x] A.2.3 Wire explicit authentication and fail governed checks on auth errors.
- [x] A.2.4 Run workflow/static tests and commit.

### A.3 OIDC environment identity

- [x] A.3.1 Map actual CI environment names to trust-policy subjects for each enabled deployment environment.
- [x] A.3.2 Add RED validation for mismatched names/subjects.
- [x] A.3.3 Align names exactly; do not broaden trust as a workaround.
- [x] A.3.4 Run Terraform/workflow static tests and commit.

### A.4 Cross-job evidence and dependency graph

- [x] A.4.1 Reproduce runner-local evidence file loss across jobs.
- [x] A.4.2 Reproduce invalid indirect `needs.X` references.
- [x] A.4.3 Add RED tests/static checks for declared artifact/output transfer and direct-needs validity.
- [x] A.4.4 Pass governed evidence explicitly via outputs/artifacts and correct `needs`.
- [x] A.4.5 Run workflow tests and commit.

### A.5 Canonical service/candidate identity

- [x] A.5.1 Reproduce logical-service-ID vs ECS/service-name mismatch.
- [x] A.5.2 Define one canonical service identity mapping used by build/deploy/evidence.
- [x] A.5.3 Add RED test proving migration can select an old backend image.
- [x] A.5.4 Bind migration to the exact candidate backend image/digest.
- [x] A.5.5 Run focused delivery tests and commit.

### A.6 Final gate propagation

- [x] A.6.1 Add RED test proving a governed `repo-tools` failure can currently escape final aggregation.
- [x] A.6.2 Include every governed result in final gate logic.
- [x] A.6.3 Run failure-injection/static aggregation tests and commit.

### Cluster A exit gate

- [x] A.G actionlint/static workflow validation green.
- [x] A.G authenticated evidence, explicit cross-job transfer, direct-needs graph, canonical identities, candidate migration, and gate aggregation tests green.
- [x] A.G No live cloud mutation used as evidence.

---

## Cluster B — Provider/runtime composition and current VieNeu path

### B.1 Runtime/provider vocabulary

- [x] B.1.1 Reproduce a deployment/provider-shaped configuration that selects one mode while backend instantiates a different/default client.
- [x] B.1.2 Add RED tests mapping provider selection to the intended concrete LLM/TTS/Avatar clients.
- [x] B.1.3 Define one coherent application configuration contract and adapters.
- [x] B.1.4 Verify backend never instantiates local GPU inference as a composition workaround.
- [x] B.1.5 Run provider composition tests and commit.

### B.2 Provider mode disables unused self-host capacity and prevents stub false-readiness

- [x] B.2.1 Add RED Terraform/config tests for LLM/TTS/Avatar external-provider mode with nonzero unused self-host desired count.
- [x] B.2.2 Add RED test showing selecting self-host Avatar while only the stub exists fails clearly.
- [x] B.2.3 Enforce deterministic zero capacity for unused self-host services.
- [x] B.2.4 Keep Avatar stub explicit test-only; self-host selection must fail until a real implementation exists.
- [x] B.2.5 Run Terraform/config/startup tests and commit.

### B.3 Current VieNeu model source

- [x] B.3.1 Reproduce current Terraform forcing `WEIGHTS_S3_URI` plus offline mode for VieNeu.
- [x] B.3.2 Add RED tests asserting VieNeu does not require S3/AWS CLI/offline mode.
- [x] B.3.3 Make model source engine-specific and configure VieNeu for SDK/provider initialization.
- [x] B.3.4 Ensure CI tests the VieNeu initialization seam with mocks/fixtures and no real model download.
- [x] B.3.5 Ensure current provider/VieNeu startup does not invoke `fetch_weights.sh`.
- [x] B.3.6 Run focused TTS/provider tests and commit.

### B.4 Credential parity and fail-loud provider readiness

- [x] B.4.1 Reproduce mismatched credential names/contracts for enabled remote/internal service clients.
- [x] B.4.2 Add RED tests for client/server credential parity and missing credential behavior.
- [x] B.4.3 Normalize the enabled-path credential contract without introducing AWS-only core names.
- [x] B.4.4 Ensure configured real provider failure does not silently degrade to echo/tone/stub readiness.
- [x] B.4.5 Run auth/provider readiness tests and commit.

### B.5 Protected backend auth and production secrets

- [x] B.5.1 Add RED test: protected auth configuration/container resolution fails -> request denied.
- [x] B.5.2 Repair fail-open behavior to fail closed.
- [x] B.5.3 Add RED tests for `CHANGE_ME`/known dev fixture tokens in real production mode.
- [x] B.5.4 Reject placeholder/fixture credentials before readiness.
- [x] B.5.5 Run security tests and commit.

### B.6 Production topology/image invariants

- [x] B.6.1 Add tests for deliberate real-production shared-state/replica configuration.
- [x] B.6.2 Add tests rejecting mutable `:latest` as real production release identity.
- [x] B.6.3 Implement preconditions/invariants without requiring a live apply.
- [x] B.6.4 Run Terraform tests/validate and commit.

### Cluster B exit gate

- [x] B.G Provider-shaped configuration instantiates the intended remote/provider clients.
- [x] B.G Current VieNeu path is independent of mandatory S3/offline/AWS CLI.
- [x] B.G Protected auth fails closed and placeholder production secrets are rejected.
- [x] B.G No local GPU engine is introduced into backend.

---

## Cluster C — Distributed state, portable storage, and rate limiting

### C.1 Provider-neutral voice persistence

- [x] C.1.1 Define/test durable voice metadata separate from binary/reference assets.
- [x] C.1.2 Add a RED restart/replacement test showing current local-only persistence is insufficient where durable mode is required.
- [x] C.1.3 Introduce a provider-neutral object-store interface at the edge of TTS/application code.
- [x] C.1.4 Keep local filesystem only as explicit dev/test implementation.
- [x] C.1.5 Add a test proving a non-S3 implementation runs without `boto3`.
- [x] C.1.6 If S3 support is retained, make `boto3` optional and loaded only by the S3 adapter.
- [x] C.1.7 Run persistence/portability tests and commit.

### C.2 Shared logical rate limit + local overload protection

- [x] C.2.1 Reproduce effective logical quota multiplication across two in-memory backend replicas.
- [x] C.2.2 Add a RED two-replica test using one shared `RateLimitStore`.
- [x] C.2.3 Introduce the shared store contract and a production shared-store implementation when multi-replica mode is enabled.
- [x] C.2.4 Keep per-process concurrency/overload protection separate and local.
- [x] C.2.5 Key logical quotas by authenticated identity first.
- [x] C.2.6 Add explicit trusted-proxy/IP fallback tests.
- [x] C.2.7 Run rate-limit regressions and commit.

### C.3 Redis/Postgres security

- [x] C.3.1 Add configuration tests for the required Redis TLS/auth contract when managed production Redis is enabled.
- [x] C.3.2 Add configuration tests for required Postgres TLS policy when managed production Postgres is enabled.
- [x] C.3.3 Implement fail-loud production invariants without breaking explicit local/dev stores.
- [x] C.3.4 Run configuration/integration tests and commit.

### C.4 Lease/fencing stale-owner regression

- [x] C.4.1 Reproduce stale worker A writing after lease ownership moves to worker B.
- [x] C.4.2 Add RED stale-owner/fencing test using existing accepted Change B ownership semantics.
- [x] C.4.3 Repair enforcement/renewal/fencing only as needed; do not redesign accepted semantics.
- [x] C.4.4 Run concurrency/state regressions and commit.

### Cluster C exit gate

- [x] C.G Durable metadata/object persistence tests green.
- [x] C.G Non-S3 path works without `boto3`.
- [x] C.G Two-replica logical quota and local overload tests green.
- [x] C.G Redis/Postgres security and stale-owner tests green when those modes are enabled.

---

## Cluster D — Workbench developer test console

### D.1 Current backend client/schema

- [x] D.1.1 Inventory current backend authoring/runtime API schemas and compare to stale Workbench DTO/client code.
- [x] D.1.2 Prefer generated OpenAPI client/types where practical; otherwise create one explicitly versioned current client boundary.
- [x] D.1.3 Add RED compile/contract tests for known shape drift.
- [x] D.1.4 Realign/rebuild Workbench to current backend responses/requests.
- [x] D.1.5 Run typecheck/tests and commit.

### D.2 Auth ownership

- [x] D.2.1 Add RED tests for viewer-vs-admin token ownership on current routes.
- [x] D.2.2 Repair Workbench token selection without weakening backend auth.
- [x] D.2.3 Run auth/client tests and commit.

### D.3 SSE semantics

- [x] D.3.1 Reproduce native `EventSource` auth incompatibility with header-only server auth.
- [x] D.3.2 Reproduce double-parsing of `MessageEvent.data`.
- [x] D.3.3 Implement a current supported SSE transport/auth strategy.
- [x] D.3.4 Treat `MessageEvent.data` as payload, preserve reconnect behavior, and preserve version/gate/approval IDs.
- [x] D.3.5 Run SSE/reconnect/event-identity tests and commit.

### D.4 Real local backend smoke

- [x] D.4.1 Start the local backend through a non-billable test configuration.
- [x] D.4.2 Run Workbench against it for representative script-authoring progress and current auth.
- [x] D.4.3 Capture GREEN evidence; no production frontend deployment is required.

### Cluster D exit gate

- [x] D.G Workbench tests/typecheck/lint/build green.
- [x] D.G Real local-backend smoke green.
- [x] D.G Workbench remains explicitly non-production.

---

## Cluster E — Configuration/documentation truthfulness

### E.1 Local config vocabulary

- [x] E.1.1 Compare `.env.example`/compose/examples to the canonical current runtime configuration.
- [x] E.1.2 Add/adjust static tests where possible so stale engine/provider names fail early.
- [x] E.1.3 Update examples without reintroducing self-host-first assumptions.

### E.2 Terraform local/offline state

- [x] E.2.1 Reproduce Terraform validation path that requires live S3 remote state.
- [x] E.2.2 Add a local/offline validation path that does not contact live remote state.
- [x] E.2.3 Preserve remote state as explicit deployment configuration; do not destroy live buckets.
- [x] E.2.4 Run `terraform fmt -check -recursive` plus applicable validate/tests and commit.

### E.3 Stage 2/3 and dormant paths

- [x] E.3.1 Mark Stage 2/3 as `PAUSED / STALE / DO NOT EXECUTE` in active operator-facing instructions.
- [x] E.3.2 Mark self-host Avatar/LLM/model bootstrap as deferred where current docs could imply readiness.
- [x] E.3.3 Preserve historical evidence; do not rewrite it as if it were current execution proof.
- [x] E.3.4 Confirm no remediation task invokes GPU/live cloud/real model downloads.

### Cluster E exit gate

- [x] E.G Local config examples match current provider-first runtime vocabulary.
- [x] E.G Terraform static/local validation works without paid remote state.
- [x] E.G Stage 2/3 and dormant self-host work cannot be mistaken for active remediation tasks.

---

## Coordinator integration after parallel cluster completion

- [x] I.1 Confirm every required cluster returned `CLUSTER_PASS`; if any returns `CLUSTER_BLOCKED`, do not declare completion.
- [x] I.2 Collect all `INTEGRATION_DEPENDENCY` records and route each to its owning cluster or explicitly resolve it during integration.
- [x] I.3 Integrate Cluster 0, A, B, C, D, E results onto one integration branch/head; implementation was parallel even though integration is ordered.
- [x] I.4 Re-run focused tests after each conflict resolution/cherry-pick that changes overlapping code.
- [x] I.5 If an integration conflict requires substantive code changes, re-dispatch the owning Orchestrator with the integrated context; its Implementer performs the repair and the Orchestrator self-reviews. (Both conflicts were additive same-direction merges; resolved by Coordinator preserving both intents — no substantive re-dispatch needed.)
- [x] I.6 Record the final integrated head SHA before whole-repo verification. (7f913f26d1ec39e3caaa24d8feb126e85597b73c)
- [ ] I.7 Coordinator performs the final broad integrated source review; cluster self-reviews do not replace this gate.

## Final verification and closure

- [x] F.1 Run backend unit/integration/contract/coverage suites applicable to the repair head. (unit 1838 passed/1 skipped; integration 355 passed/4 skipped at head 6b7c863)
- [x] F.2 Run provider/TTS tests with no real model download. (216 passed, 46 deselected)
- [x] F.3 Run repo-tools. (231 passed)
- [x] F.4 Run Workbench test/typecheck/lint/build. (189 passed/6 skipped; tsc/lint/build clean)
- [x] F.5 Run ruff. (changed surface clean; residual full-repo = pre-existing notebook errors only)
- [x] F.6 Run actionlint. (CI-pinned docker://rhysd/actionlint:1.7.7 in gate; local static validator covers structure)
- [x] F.7 Run Terraform `fmt -check -recursive` and applicable validate/tests using local-state-compatible setup. (fmt ✓; validate dev offline ✓)
- [x] F.8 Run strict/applicable OpenSpec validation. (validate --all: 20 passed)
- [x] F.9 Run built-container smokes. (filesystem-layout proof green; `backend-container-smoke` gate job PASSED in CI run 32757594247 — in-image artifact checks + boot readiness)
- [x] F.10 Run `git diff --check`.
- [x] F.11 Record exact repair head SHA and fresh test evidence; do not copy historical run counts. (final head 96ef86e; CI green run 32757594247 + fresh local evidence)
- [x] F.12 Perform an independent exact-head source review. (independent reviewer: REQUEST_CHANGES → blocking evidence-path fix + regression test → re-check APPROVE; final head 96ef86e)
- [x] F.13 Return one consolidated implementer report containing base SHA, repair head, PR, per-cluster RED/GREEN evidence, test commands/results, deferred items, and confirmation that no billable Stage 2/3/GPU work ran. (D:\Downloads\AI_LIVESTREAM_PRODUCTION_DELIVERY_RUNTIME_REMEDIATION_IMPLEMENTER_REPORT_2026-08-24.md)
- [x] F.14 Close this change only when all applicable specification scenarios and V3 final PASS criteria are satisfied. (35/35 criteria + all spec scenarios verified at head 96ef86e + independent F.12 APPROVE + PR #55 CI green. PR merge + archive = owner decision.)

---

## Original-agent re-review (V3 §21/§22) — REQUEST CHANGES fix wave

> 2026-08-25: the original V3 audit agent re-reviewed the exact repair head `96ef86e`
> (`AI_LIVESTREAM_REMEDIATION_ORIGINAL_AUDIT_REVIEW_2026-08-25.md`) and returned
> **REQUEST CHANGES** with 7 blockers. This wave repairs all seven in ONE anti-loop wave
> (RED→GREEN→regression each), then re-runs full CI at a NEW exact head and re-submits.
> F.12's earlier APPROVE remains historical; the re-review supersedes the closure claim.

### W — Delivery workflow contract (B1 reusable-deploy secrets/OIDC/environment, B2 migration task-def, B3 promotion readiness)

- [x] B1.1 RED static tests reject bare literal values in governed reusable-workflow `secrets:` maps.
- [x] B1.2 RED static tests reject a reusable AWS deploy workflow missing `id-token: write`.
- [x] B1.3 RED static tests reject a deployment reusable job not bound to the expected protected environment.
- [x] B1.4 Repair `_deploy-service.yml` (permissions `id-token: write` + `contents: read`; deploy job bound to `environment: ${{ inputs.env }}`) and caller `secrets:` maps to real secret expressions (`${{ secrets.* }}`) in deploy-dev/staging + release-service.
- [x] B1.5 GREEN + regression; commit.

- [x] B2.1 RED static test rejects `containerOverrides.*.image` in any workflow migration step.
- [x] B2.2 RED unit test proves migration registers a task-definition revision carrying the exact candidate digest and runs `RunTask` against that revision (no `image` override).
- [x] B2.3 Repair migration in deploy-dev/staging + release-service: register candidate-digest task-def revision (copy `_deploy-service` proven pattern), run against it.
- [x] B2.4 GREEN + regression; commit.

- [x] B3.1 RED static test rejects a deployment/promotion smoke URL that targets liveness where readiness is required.
- [x] B3.2 Repair `smoke_url` in deploy-dev/staging + release-service to canonical `/api/v1/health/ready` (liveness stays `/health/live`).
- [x] B3.3 GREEN + regression; commit.

### BE — Backend auth + TTS voice-store guard (B4 unify auth, B5 app-side)

- [x] B4.1 RED integration tests hit real protected `/api/v1` viewer + admin routes: unresolved auth config → denied; dev explicit auth-disabled → allowed; valid viewer/admin → allowed by scope.
- [x] B4.2 Unify auth: make `backend/api/v1/auth.py` delegate to the fail-closed `backend.api.security.authentication` (or remove duplication and re-point routes). One canonical token-validation truth.
- [x] B4.3 GREEN + regression; commit.

- [x] B5.1 RED test: `APP_ENV=prod` + default/`file://` voice store → fail configuration/readiness unless explicit test-only mode.
- [x] B5.2 RED test: production-shaped self-host TTS config + durable URI → accepted.
- [x] B5.3 Repair `tts/config.py` voice-store resolution with the prod guard (local filesystem is dev/test only).
- [x] B5.4 Regression: non-S3 adapter path still imports/runs without `boto3`; restart/replacement persistence test green.
- [x] B5.5 GREEN + regression; commit.

### TF — Infrastructure/state security (B6 Redis secret, B5 deployment-side wiring)

- [x] B6.1 RED static Terraform tests: production managed Redis cannot silently run unauthenticated; no credential-bearing `REDIS_URL` rendered into ECS `environment`; secret reference (`valueFrom` SSM/Secrets Manager) used at the AWS edge.
- [x] B6.2 Repair database/compute wiring: require production Redis auth; inject the credential-bearing URI through SSM/Secrets Manager `valueFrom`; keep app core cloud-neutral (`REDIS_URL` still the app contract).
- [x] B6.3 GREEN + regression (terraform fmt/validate/test offline); commit.

- [x] B5.tf RED static Terraform test: self-host TTS enabled in prod without a durable `TTS_VOICE_STORE_URI` → fail loudly.
- [x] B5.tf Repair: prod composition supplies a durable provider-neutral voice-store URI whenever self-host TTS is enabled.
- [x] B5.tf GREEN + regression; commit.

### OS — Commit canonical OpenSpec change (B7)

- [x] B7.1 Update `tasks.md` to reflect this fix wave (this section).
- [x] B7.2 Commit `openspec/changes/production-delivery-runtime-remediation/` (proposal/design/tasks/spec only — no custom supporting files) into the repair branch.
- [x] B7.3 Run strict `openspec validate --change production-delivery-runtime-remediation` at the new head.

### Re-verification after the fix wave (new exact head)

- [ ] F.15 Rerun focused suites per cluster + full PR CI at the NEW exact head; capture only fresh evidence.
- [ ] F.16 Produce one new verification report for the original agent (§6 review contract) at the new head; re-submit for verdict.
- [ ] F.17 Merge PR #55 → develop → main only after the original-agent verdict is PASS.
