# Production Delivery Runtime Remediation Specification

## Purpose

Cross-cutting remediation contract governing the production artifact, runtime composition, delivery trust chain, security, distributed state, developer console, and configuration truthfulness for the VN AI live-commerce backend. Repairs the audited defects at `6ff71f3` without reopening accepted Change A/B product semantics, keeping provider-first composition, portable storage seams, and non-billable verification.

## Requirements

### Requirement: Frozen accepted product semantics
The remediation SHALL preserve accepted Change A `adaptive-speech-text-chunking` and Change B `approved-script-authoring-pipeline` product semantics and SHALL limit changes to packaging, runtime composition, delivery, security, distributed state, CI, developer-console, configuration, and documentation boundaries.

#### Scenario: Packaging defect affects accepted Change B behavior
- **GIVEN** accepted Change B code requires a packaged runtime resource
- **WHEN** the production artifact omits that resource
- **THEN** the remediation SHALL repair the artifact boundary
- **AND** SHALL NOT redesign the accepted Change B generation/gate/approval semantics.

#### Scenario: Proposed repair requires semantic redesign
- **GIVEN** an implementer believes an active finding can only be fixed by changing accepted Change A/B behavior
- **WHEN** the repair reaches that boundary
- **THEN** implementation SHALL stop for that item
- **AND** the semantic redesign SHALL require a separate explicitly approved change.

### Requirement: Backend runtime resources are packaged
The production-shaped backend image SHALL contain every immutable project-owned runtime resource required by the accepted backend behavior.

#### Scenario: Script authoring resources are loaded inside the built image
- **WHEN** the real backend image is built and tested
- **THEN** `SkillLoader().content()` and `SkillLoader().content_hash()` SHALL succeed
- **AND** curated profanity and safety resources SHALL load from the built image
- **AND** no runtime network fetch of mutable skill/gate resources SHALL be required.

### Requirement: Readiness uses truthful HTTP status
Canonical readiness endpoints SHALL return HTTP `200` only when the application is ready and HTTP `503` when required runtime state or dependencies are unavailable.

#### Scenario: Required dependency is unavailable
- **GIVEN** a required runtime dependency is unavailable
- **WHEN** a readiness endpoint is called
- **THEN** it SHALL return HTTP `503`
- **AND** a JSON body SHALL NOT override the failure with a successful HTTP status.

#### Scenario: Process is alive but application is not ready
- **GIVEN** the server process is alive
- **AND** the application is not ready
- **WHEN** liveness and readiness are checked
- **THEN** liveness MAY return success
- **BUT** readiness SHALL return HTTP `503`.

### Requirement: Backend remains lightweight and provider/transport-neutral
The backend SHALL use provider/service client interfaces for LLM, TTS, and Avatar capabilities and SHALL NOT instantiate local GPU inference merely to satisfy deployment configuration.

#### Scenario: External LLM provider is selected
- **WHEN** an external/BYOK LLM provider configuration is active
- **THEN** the backend SHALL instantiate the intended remote/provider client
- **AND** SHALL NOT start vLLM or another local GPU engine in the backend process.

#### Scenario: Transport differs by adapter
- **GIVEN** two provider adapters use different transports
- **WHEN** the application calls the capability
- **THEN** the application-facing contract SHALL remain stable
- **AND** the adapter MAY use HTTP/SSE, gRPC streaming, WebSocket, or another measured transport.

### Requirement: Provider selection and self-host capacity agree
Selecting an external/provider mode SHALL force unused self-host capacity for the corresponding capability to zero.

#### Scenario: Third-party Avatar mode
- **WHEN** third-party Avatar mode is selected
- **THEN** desired self-host Avatar capacity SHALL be zero
- **AND** no self-host Avatar GPU task SHALL be required for readiness.

#### Scenario: Self-host Avatar selected while only stub exists
- **GIVEN** no real self-host Avatar runtime is production-ready
- **WHEN** self-host Avatar mode is selected
- **THEN** configuration/plan/startup SHALL fail clearly
- **AND** the stub SHALL NOT report a production-ready self-host capability.

### Requirement: Current VieNeu model loading is engine-specific
The current VieNeu-TTS engine SHALL use its SDK/provider model initialization path and SHALL NOT require S3, AWS CLI, or forced offline mode by default.

#### Scenario: VieNeu is the selected TTS engine
- **WHEN** the production-shaped TTS configuration is rendered for VieNeu
- **THEN** mandatory `WEIGHTS_S3_URI` SHALL NOT be required
- **AND** offline mode SHALL NOT be forced by default
- **AND** the VieNeu core runtime SHALL NOT require AWS CLI or `boto3`.

#### Scenario: Future self-host engine uses object storage
- **GIVEN** a future engine intentionally uses object-backed weight bootstrap
- **WHEN** that engine is activated in a separate approved change
- **THEN** its model-source implementation MAY use an object-store adapter
- **AND** it SHALL fetch only artifacts required by that engine.

### Requirement: Generic self-host weight bootstrap is not an active dependency
The current provider-first/VieNeu runtime SHALL start and pass non-GPU CI without invoking the generic self-host weight bootstrap.

#### Scenario: Current provider paths boot
- **WHEN** current LLM provider, VieNeu, and Avatar provider paths are exercised in CI
- **THEN** `fetch_weights.sh` SHALL NOT be required
- **AND** no real model download SHALL occur.

### Requirement: Voice persistence is provider-neutral
Voice metadata SHALL use a durable metadata store and binary/reference assets SHALL use a provider-neutral object-storage interface.

#### Scenario: Voice assets survive process replacement
- **GIVEN** a persisted voice profile
- **WHEN** the TTS process restarts or is replaced
- **THEN** metadata SHALL remain available
- **AND** referenced binary assets SHALL remain retrievable from the configured durable object store.

#### Scenario: Non-S3 implementation is selected
- **WHEN** a non-S3 object-store implementation is configured
- **THEN** core TTS/application functionality SHALL run without `boto3`
- **AND** AWS SDK dependencies SHALL remain optional adapter dependencies.

### Requirement: Protected authentication fails closed
Protected backend routes SHALL deny access when authentication configuration/container resolution fails.

#### Scenario: Auth configuration cannot be resolved
- **WHEN** a protected route cannot resolve required authentication configuration
- **THEN** access SHALL be denied
- **AND** the failure SHALL NOT silently disable authentication.

### Requirement: Production credentials cannot be placeholders
Real production mode SHALL reject known placeholder/development fixture credentials.

#### Scenario: Placeholder credential is configured
- **GIVEN** real production mode
- **WHEN** a required credential equals a known placeholder or development fixture value
- **THEN** startup/configuration SHALL fail
- **AND** the service SHALL NOT become ready.

### Requirement: Reusable GitHub workflows are structurally valid
Reusable GitHub workflows SHALL be invoked according to GitHub job-level reusable-workflow syntax and SHALL pass static workflow validation.

#### Scenario: CI validates workflows
- **WHEN** workflow files are changed
- **THEN** `actionlint` or equivalent structural validation SHALL run
- **AND** no reusable workflow SHALL be invoked through `steps[*].uses`.

### Requirement: Delivery evidence is authenticated and explicitly transferred
GitHub API evidence calls SHALL be explicitly authenticated and cross-job evidence SHALL move through declared outputs/artifacts.

#### Scenario: Job B consumes evidence produced by Job A
- **WHEN** Job B requires evidence produced by Job A
- **THEN** the data SHALL be passed through a declared artifact or output
- **AND** Job B SHALL NOT assume Job A's runner-local filesystem is shared.

#### Scenario: Workflow calls GitHub API
- **WHEN** `gh api` is used for governed evidence
- **THEN** the command SHALL have an explicit token environment contract
- **AND** authentication failure SHALL fail the governed check rather than be interpreted as absence of evidence.

### Requirement: Workflow dependency references are graph-valid
A job SHALL reference `needs.X` only when `X` is a direct declared dependency of that job.

#### Scenario: Evidence aggregation reads prior job result
- **WHEN** a job reads another job's result/output
- **THEN** that job SHALL declare the producer in its `needs`
- **AND** static tests SHALL reject invalid indirect references.

### Requirement: Release service and migration identity is exact
Delivery automation SHALL use canonical service identities and the exact candidate backend image identity for migrations/release evidence.

#### Scenario: Candidate migration runs
- **WHEN** a backend candidate requires migration
- **THEN** migration SHALL run with the exact candidate backend image identity
- **AND** SHALL NOT use an older task definition image by accident.

### Requirement: Final delivery gate propagates governed failures
The final CI/release gate SHALL fail if any governed validation fails.

#### Scenario: repo-tools fails
- **GIVEN** `repo-tools` is a governed prerequisite
- **WHEN** it fails
- **THEN** the final gate SHALL fail
- **AND** success SHALL NOT be reported by omitting that result from aggregation.

### Requirement: OIDC environment identity is exact when cloud deployment is enabled
For an enabled cloud deployment, CI environment names and cloud trust-policy subjects SHALL match exactly.

#### Scenario: Deployment environment name differs from trust subject
- **WHEN** CI and cloud trust use different environment identifiers
- **THEN** validation SHALL fail before deployment
- **AND** the mismatch SHALL NOT be bypassed by broadening trust to unrelated environments.

### Requirement: Built-container smoke tests are mandatory
CI SHALL build and boot production-shaped containers for cheap artifact/startup/readiness checks without GPU, large model downloads, or live cloud mutation.

#### Scenario: Backend container smoke
- **WHEN** the backend image is built in CI
- **THEN** packaged resource loads SHALL be tested inside the container
- **AND** liveness/readiness behavior SHALL be tested
- **AND** a no-cloud/no-GPU authoring preflight SHALL reach the intended generation seam.

#### Scenario: Shared executable file changes
- **WHEN** a shared executable/config file changes
- **THEN** every affected service smoke/test SHALL run
- **AND** CI SHALL not test only one consumer of the shared file.

### Requirement: Logical rate limits are shared across replicas
A logical user/account/API quota SHALL be enforced through a shared `RateLimitStore` across backend replicas, while overload/concurrency protection MAY remain local per replica.

#### Scenario: Same identity reaches two replicas
- **GIVEN** two backend replicas share a logical rate-limit store
- **WHEN** the same authenticated identity sends requests through both replicas
- **THEN** the combined requests SHALL consume one logical quota
- **AND** effective quota SHALL NOT multiply by replica count.

#### Scenario: Replica protects local capacity
- **WHEN** one backend replica reaches its local concurrency/overload limit
- **THEN** that replica MAY reject/throttle locally
- **AND** this local protection SHALL remain distinct from the shared user quota.

### Requirement: Authenticated identity is preferred over client IP for quotas
Rate-limit identity SHALL prefer authenticated account/user/API-key/tenant identity. IP SHALL be fallback only under an explicit trusted-proxy policy.

#### Scenario: Authenticated user behind shared NAT
- **GIVEN** multiple authenticated users share one public IP
- **WHEN** quotas are evaluated
- **THEN** each user's logical quota SHALL be keyed by authenticated identity rather than the shared IP.

### Requirement: Managed production stores enforce security and ownership correctness
When managed Redis/Postgres are enabled for real multi-replica production, connection security and stale-owner protection SHALL be enforced.

#### Scenario: Redis/Postgres production mode
- **WHEN** managed production data stores are enabled
- **THEN** the configured TLS/auth policy SHALL meet the project production contract
- **AND** plaintext/unauthenticated fallbacks SHALL not silently become ready.

#### Scenario: Lease expires while stale worker continues
- **GIVEN** worker A loses a session/work lease and worker B becomes the current owner
- **WHEN** worker A later attempts a stale write
- **THEN** fencing/ownership validation SHALL reject the stale write.

### Requirement: Workbench is a current-backend developer test console
Workbench SHALL be treated as a developer test console and SHALL use the current backend contract rather than receiving production-frontend hardening.

#### Scenario: Backend schema changes
- **WHEN** Workbench is built against the current backend
- **THEN** its client/types SHALL match the current backend contract
- **AND** generated OpenAPI-derived types/client SHOULD be preferred where practical to reduce drift.

#### Scenario: Script-authoring progress is observed
- **WHEN** Workbench consumes current SSE progress
- **THEN** authentication SHALL match backend ownership
- **AND** `MessageEvent.data` SHALL be handled as the event payload
- **AND** reconnect plus version/gate/approval identities SHALL be preserved.

### Requirement: Local Terraform validation does not require live remote state
Development/offline Terraform checks SHALL be runnable without requiring a live S3 remote backend.

#### Scenario: Developer validates Terraform offline
- **WHEN** a developer or CI runner performs static/local Terraform validation
- **THEN** validation SHALL not require access to paid remote state infrastructure
- **AND** real remote-state resources SHALL not be automatically destroyed.

### Requirement: Production image identity is immutable when production deployment is enabled
When real production deployment is enabled, service image identity SHALL use an immutable digest or equivalently immutable reference.

#### Scenario: Production release candidate is selected
- **WHEN** production deployment is prepared
- **THEN** candidate service images SHALL be immutable
- **AND** mutable `:latest` SHALL NOT be the release identity.

### Requirement: Application-core architecture remains cloud-portable
Application-core contracts SHALL not require AWS as the only storage/compute provider.

#### Scenario: Object storage implementation changes
- **WHEN** an object-store adapter is replaced with a non-AWS implementation
- **THEN** application-core voice/provider semantics SHALL remain unchanged
- **AND** AWS-specific dependencies SHALL remain at the adapter/infrastructure edge.

### Requirement: Stage 2/3 and billable self-host validation remain paused
This remediation SHALL NOT execute stale Stage 2/3 work, GPU tests, real model downloads, or live cloud mutation by default.

#### Scenario: Implementer reaches a Stage 2/3 task
- **WHEN** an archived/active historical Stage 2/3 instruction is encountered
- **THEN** it SHALL be treated as PAUSED/STALE/DO NOT EXECUTE
- **AND** the implementer SHALL use the current remediation requirements instead.

#### Scenario: Current remediation evidence is collected
- **WHEN** implementation is verified
- **THEN** ordinary CI/local fixtures/mocks SHALL be used
- **AND** no billable GPU/cloud run SHALL be required as PASS evidence.

### Requirement: Parallel cluster execution uses bounded two-level delegation
The remediation SHALL execute independent clusters concurrently through isolated worktrees/branches while enforcing the maximum delegation topology `Coordinator -> Orchestrator -> Implementer`.

#### Scenario: Coordinator starts remediation clusters
- **GIVEN** one recorded common base SHA
- **WHEN** remediation implementation begins
- **THEN** Cluster 0, A, B, C, D, and E MAY be dispatched concurrently in isolated worktrees/branches
- **AND** parallel implementation SHALL NOT require them to share a mutable working tree.

#### Scenario: Agent delegation depth is evaluated
- **WHEN** an Orchestrator needs implementation work
- **THEN** it MAY dispatch/resume a focused Implementer
- **AND** the Orchestrator SHALL NOT spawn another orchestrator, coordinator, or reviewer subagent
- **AND** an Implementer SHALL NOT spawn any subagent.

#### Scenario: Implementer returns work
- **WHEN** an Implementer returns a diff/report
- **THEN** its Orchestrator SHALL review that work itself for spec compliance and quality
- **AND** a failed review SHALL trigger an implementer fix round before cluster PASS
- **AND** no more than five fix rounds SHALL be used for one task.

#### Scenario: Parallel clusters need the same owned domain
- **GIVEN** a cluster discovers a required change owned by another cluster
- **WHEN** making the change locally would violate ownership or create avoidable merge conflict
- **THEN** the Orchestrator SHALL return an `INTEGRATION_DEPENDENCY`
- **AND** the Coordinator SHALL route the dependency to the owning cluster or resolve it during integration.

#### Scenario: Cluster is complete
- **WHEN** all tasks in a cluster pass its Orchestrator review
- **THEN** the Orchestrator SHALL return `CLUSTER_PASS` with exact head, commits, changed files, RED/GREEN/regression evidence, review/fix ledger, integration dependencies, and scope confirmations
- **AND** only the Coordinator SHALL integrate cluster branches.

#### Scenario: Integrated completion is evaluated
- **WHEN** all required cluster branches have been integrated
- **THEN** fresh exact-integrated-head verification SHALL run
- **AND** the Coordinator SHALL perform the broad integrated review
- **AND** no cluster-level PASS alone SHALL be sufficient to close the remediation.

### Requirement: Final exact-head verification and independent review are required
The remediation SHALL not be declared complete until fresh applicable verification is green at the exact repair head and an independent source review approves that head.

#### Scenario: Historical CI evidence exists
- **GIVEN** historical CI runs passed on earlier commits
- **WHEN** the remediation is evaluated
- **THEN** historical counts SHALL NOT substitute for fresh exact-head evidence.

#### Scenario: Repair head is ready for closure
- **WHEN** all applicable focused/full suites are green
- **AND** the exact repair head receives an independent source review with no unresolved active blocker
- **THEN** the remediation MAY be considered complete.
