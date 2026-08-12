# branch-governed-service-delivery Specification

## Purpose
TBD - created by archiving change refactor-microservices-monorepo. Update Purpose after archive.
## Requirements
### Requirement: Canonical branch integration path
The repository SHALL use `feature/*` branches for feature development, `develop` as the integration branch, and `main` as the releasable branch. Feature work MUST enter `develop` through a pull request, and releasable work MUST enter `main` through a pull request from `develop`. Repository rules on `develop` and `main` MUST require the stable `CI / gate`, at least one approving review, resolution of all review conversations, and a conflict-free head current with the target branch. Feature branches MUST NOT integrate directly into `main`, and direct protected-branch pushes MUST be denied except through an audited emergency bypass.

#### Scenario: Integrate a feature
- **WHEN** a developer completes work on a `feature/*` branch
- **THEN** the supported integration target is `develop` through a pull request with required CI checks

#### Scenario: Prepare a release
- **WHEN** integrated work is ready to become releasable
- **THEN** the supported release path is a pull request from `develop` to `main` with required CI checks

#### Scenario: Block an unready pull request
- **WHEN** a pull request lacks `CI / gate`, an approval, resolved conversations, or a current conflict-free head
- **THEN** repository rules prevent it from merging into `develop` or `main`

#### Scenario: Reject direct feature release
- **WHEN** a `feature/*` branch targets `main` or attempts a direct protected-branch push
- **THEN** repository rules reject the integration unless an authorized emergency bypass is used and audited

### Requirement: Event-specific repository-aware CI
`ci.yml` SHALL run for governed feature, `develop`, and `main` push and pull-request events, SHALL scan secrets before accepting the gate, and SHALL compute an affected-area map without path-skipping the required workflow. Service-owned contract artifacts, dependency locks, shared configuration, or CI/build changes MUST fan out to their affected consumers. Every mode MUST finish with one stable `CI / gate`, including when unaffected jobs are skipped.

#### Scenario: Push a feature branch
- **WHEN** a commit is pushed to `feature/*`
- **THEN** CI runs secret scanning, affected-area detection, format, lint, typecheck, and unit tests for changed product services and runs no integration, contract, coverage, full-container, or deployment job

#### Scenario: Validate a feature pull request
- **WHEN** a `feature/*` pull request targets `develop`
- **THEN** CI runs secret scanning plus affected format, lint, typecheck, unit, integration, contract, coverage, cached container validation with `push: false`, and conditional workbench, platform, and Terraform checks before reporting `CI / gate`

#### Scenario: Verify a develop merge commit
- **WHEN** a feature pull request merges into `develop`
- **THEN** CI reruns full affected-area integration checks and cached affected-image validation with `push: false` against the exact merge commit and deploys no environment

#### Scenario: Validate a release pull request
- **WHEN** a pull request from `develop` targets `main`
- **THEN** CI runs repository-aware release verification with affected service, container, workbench, platform, and infrastructure checks before reporting `CI / gate`

#### Scenario: Verify a main merge commit
- **WHEN** a release pull request merges into `main`
- **THEN** CI reruns release verification against the exact merge commit without pushing an image or deploying an environment

#### Scenario: Change a consumed input
- **WHEN** a service-owned contract artifact, dependency lock, shared configuration, or CI/build definition changes
- **THEN** affected-area detection selects every known consumer rather than only the file's nearest service

### Requirement: Canonical service ownership layout
All deployable runtime definitions SHALL live under `services/`. Project-owned source services MUST live under `services/product/`, while configuration and deployment assets for upstream runtimes MUST live under `services/platform/`. Terraform cloud provisioning SHALL remain under `infra/` and MUST NOT duplicate service runtime configuration.

#### Scenario: Locate a project-owned service
- **WHEN** a contributor needs to modify backend, LLM, TTS, or avatar implementation code
- **THEN** the service is located under `services/product/<name>_service/` and owns its source, package metadata, container definition, and service-local scripts

#### Scenario: Locate an upstream runtime
- **WHEN** a contributor needs to configure LiveKit, LMCache, Postgres, or Redis
- **THEN** its runtime configuration is located under `services/platform/<name>/` without vendored upstream source

#### Scenario: Provision cloud infrastructure
- **WHEN** a contributor changes Terraform resources used to host a product or platform service
- **THEN** the Terraform change remains under `infra/` and references rather than duplicates the service runtime definition

### Requirement: Real and reproducible platform runtimes
Local/sandbox LiveKit and optional LMCache platform images MUST derive from tested upstream images pinned by immutable version and digest. Platform wrappers MUST contain only required configuration, entrypoint behavior, validation, and smoke tooling; they MUST NOT vendor upstream source or add project business logic or product APIs. A required package, binary, credential, or configuration failure MUST fail build or startup and MUST NOT be converted into a warning, ignored exit status, substitute process, or synthetic successful health response. Development, staging, and production media delivery MUST use LiveKit Cloud until a separately approved VM-based self-host design satisfies direct UDP, TURN/TLS, public reachability, and stable-capacity requirements; the current LiveKit Fargate Spot service MUST NOT remain in the target topology.

#### Scenario: Build a platform image
- **WHEN** CI builds LiveKit or LMCache
- **THEN** the build resolves the approved pinned upstream image and fails if required dependencies, copied configuration, or validation are unavailable

#### Scenario: Start LiveKit without credentials
- **WHEN** the LiveKit runtime lacks a required API key or secret
- **THEN** its entrypoint exits non-zero before accepting signaling or media traffic

#### Scenario: Select a non-local LiveKit runtime
- **WHEN** development, staging, or production is provisioned under the approved Stage 2/3 topology
- **THEN** backend and avatar publishing use LiveKit Cloud and Terraform allocates no LiveKit Fargate task or Spot capacity

#### Scenario: Probe an unavailable upstream runtime
- **WHEN** the LiveKit or LMCache upstream process or binary is absent or unhealthy
- **THEN** the real readiness probe fails and no fallback application reports the runtime as healthy

### Requirement: Evidence-gated LMCache topology
LMCache SHALL default to disabled with desired count zero. Enabling it MUST require a benchmark against the actual LLM workload that demonstrates a material latency, throughput, or GPU-cost benefit. The enabled LMCache multiprocess server MUST run from the pinned upstream standalone runtime on capacity compatible with vLLM and MUST be colocated with the LLM GPU workload when required for IPC and node-local cache sharing. It MUST NOT run as an independent ARM CPU skeleton or expose a project-written fake metrics service.

#### Scenario: Run the initial low-load topology
- **WHEN** the environment uses one LLM replica or no approved LMCache benchmark exists
- **THEN** LMCache remains disabled and no LMCache compute capacity is allocated

#### Scenario: Enable LMCache after measurement
- **WHEN** staging benchmark evidence meets the approved cache-benefit threshold
- **THEN** deployment enables the real LMCache server on the compatible LLM GPU topology and verifies vLLM connector, IPC, health, metrics, and cache behavior before promotion

#### Scenario: Lose the real LMCache binary
- **WHEN** the upstream LMCache server cannot start
- **THEN** the deployment and smoke test fail rather than starting a placeholder HTTP or metrics process

### Requirement: Managed Postgres and Redis boundaries
Local development and ephemeral smoke tests SHALL use official Postgres and Redis images without project-owned production image forks. Staging and production SHALL provision managed RDS and ElastiCache through `infra/modules/database/`. Backend-owned runtime schema and raw SQL MUST remain under `backend/db/sql/` and MUST NOT be copied into `services/platform/postgres/`. Postgres and Redis data ports MUST remain unreachable from the public Internet, and staging/production access MUST use security-group isolation, authentication, and encryption supported by the managed service. Development SHALL default both managed data resources off and use memory sessions unless an explicit infrastructure test enables them.

#### Scenario: Run local data dependencies
- **WHEN** a developer starts the local runtime matrix
- **THEN** it starts official Postgres and Redis images using local-only configuration and verifies them with platform smoke scripts

#### Scenario: Provision staging or production data dependencies
- **WHEN** Terraform provisions a non-local environment
- **THEN** it creates or references managed RDS and ElastiCache resources without building custom Postgres or Redis production images

#### Scenario: Share Redis economically
- **WHEN** the initial staging or production topology uses managed Redis
- **THEN** the deployment serves backend runtime/session ownership only while LiveKit Cloud owns media-plane persistence, and Multi-AZ or a split is added only after availability, capacity, or noisy-neighbor evidence justifies it

### Requirement: Platform-specific CI
Changes under `services/platform/` SHALL run platform validation rather than product-service typecheck and coverage. LiveKit CI MUST validate YAML and shell, lint and build its container, verify the pinned image, scan vulnerabilities, and smoke the real process. LMCache CI MUST validate configuration, verify the pinned image, build and scan the container, and smoke the real non-GPU process; GPU integration and benchmarking MUST run only in explicitly authorized staging. Postgres and Redis CI MUST validate configuration, scan the official images, and run ephemeral smoke checks.

#### Scenario: Validate a platform pull request
- **WHEN** a trusted pull request changes a platform runtime
- **THEN** `CI / gate` requires the matching config, pin, build or image, vulnerability, and real readiness checks without scheduling routine GPU work

#### Scenario: Validate LMCache with GPU
- **WHEN** an authorized staging run evaluates LMCache
- **THEN** it runs the GPU integration and workload benchmark and records evidence without adding GPU cost to ordinary pull-request CI

### Requirement: Concern-based Terraform layout and isolated environments
Terraform modules MUST remain organized as `network`, `security`, `compute`, `database`, `loadbalancer`, `storage`, `secrets`, and `monitoring`. The compute module MUST split shared cluster/IAM/discovery resources and backend, LLM, TTS, and avatar resources into separate `.tf` files inside the same module without introducing service-named Terraform modules or changing resource addresses solely because files moved. Environment roots and remote state MUST be independent for `global`, `dev`, `staging`, and `prod`; Terraform workspaces MUST NOT provide environment isolation.

#### Scenario: Refactor the compute god file
- **WHEN** the current `infra/modules/compute/main.tf` is decomposed
- **THEN** resources move to `cluster.tf`, `iam.tf`, `discovery.tf`, `backend.tf`, `llm.tf`, `tts.tf`, or `avatar.tf` inside the same module and an unchanged resource retains its Terraform address

#### Scenario: Plan staging independently
- **WHEN** CI or an operator targets staging
- **THEN** Terraform uses `infra/environments/staging/` and the staging state key without selecting a workspace or reading dev/prod state

### Requirement: Independently deployable and cost-bounded compute
Each environment SHALL use one ECS cluster with independent backend, LLM, TTS, and avatar services. Backend SHALL use Fargate Spot only in development and on-demand Fargate in staging and production, with two backend tasks in the initial production topology. Each self-host model service MUST own its task definition, desired count, capacity provider, health check, deployment, and rollback; LLM and TTS MUST NOT share a task definition or rely on fractional ECS GPU ownership. Selecting a hosted adapter MUST force the corresponding self-host desired count and minimum capacity to zero. LMCache MUST NOT own a standalone ECS service or ASG and MAY run only colocated with the LLM topology after approved benchmark evidence.

#### Scenario: Select a hosted provider
- **WHEN** backend selects a hosted LLM, TTS, or avatar adapter
- **THEN** the matching self-host service has desired count zero and allocates no idle GPU capacity

#### Scenario: Deploy two self-host services
- **WHEN** LLM and TTS are both selected for self-hosting
- **THEN** each has an independently deployable service and capacity contract rather than a combined `llm_tts` task

#### Scenario: Run the production backend
- **WHEN** production reaches steady state
- **THEN** two on-demand backend tasks serve through the public ALB without relying on Spot capacity

### Requirement: Minimal internal service networking
Only backend SHALL be exposed through the public ALB. Backend MUST resolve internal LLM, TTS, and avatar services through Cloud Map private DNS with service authentication and container health checks. The initial zero-or-one-replica model topology MUST NOT allocate internal model-service NLBs or a Service Connect proxy; adding a proxy SHALL require multiple-replica, retry, or traffic-observability evidence.

#### Scenario: Call a self-host model
- **WHEN** backend uses an internal adapter
- **THEN** it calls the environment-specific Cloud Map hostname and no public or internal model-service load balancer is required

### Requirement: Immutable and rollback-safe runtime deployment
Every development, staging, and production service deployment MUST identify its image by immutable digest. Production MUST promote the exact staging-verified digest without rebuilding. ECS services MUST use health checks and deployment circuit-breaker rollback. Runtime database migration MUST execute once as a pre-deploy backend task and MUST be additive/backward-compatible with the previous application revision.

#### Scenario: Promote production
- **WHEN** a service passes staging verification and production approval
- **THEN** production receives the recorded staging digest and not a mutable tag or rebuilt image

#### Scenario: Fail a rollout
- **WHEN** new tasks do not reach healthy steady state or smoke verification fails
- **THEN** deployment restores the prior task definition/digest for only the affected service

### Requirement: Protected infrastructure state and mutation
Terraform CI MUST run recursive format checks, backend-free initialization, validation, native tests, and trusted plans without applying. Infrastructure apply MUST be a protected manual dispatch against an exact commit and reviewed saved plan. A separate teardown dispatch MAY target only `dev` or `staging`, MUST require typed confirmation and approval, and MUST reject production. Service deployment workflows MUST NOT run Terraform apply.

Remote state MUST use encrypted, versioned, non-public S3 objects with separate environment keys and native S3 lockfiles. GitHub OIDC roles MUST restrict repository, GitHub environment, allowed action, and resource scope. Plaintext application/provider/API tokens MUST NOT enter Terraform variables or state; protected GitHub Environment secrets SHALL update SSM SecureString values out of band and ECS SHALL reference their ARNs.

#### Scenario: Validate an infrastructure pull request
- **WHEN** a pull request changes `infra/**`
- **THEN** CI validates and plans the affected roots without mutating cloud resources

#### Scenario: Apply infrastructure
- **WHEN** an authorized operator dispatches `infra-apply.yml` for an exact commit and environment
- **THEN** the workflow obtains environment approval and applies the reviewed saved plan independently of service deployment

#### Scenario: Tear down non-production
- **WHEN** an authorized operator dispatches teardown with typed confirmation
- **THEN** only dev or staging can be destroyed and production selection fails before Terraform execution

#### Scenario: Consume an application secret
- **WHEN** an ECS task requires an API, provider, LiveKit, viewer, or admin token
- **THEN** Terraform state contains only the approved parameter reference and the task resolves the secret from SSM at runtime

### Requirement: Developer-only modular Workbench
The visual test console SHALL live under `workbench/` and SHALL use Vite, vanilla TypeScript, Tailwind CSS, Vitest, and Playwright without React, Vue, Next.js, or a Workbench application server. It MUST expose one `index.html`; runtime behavior MUST be split into flat modules for API transport, API types, WebSockets, state, sessions, resource discovery, diagnostics, LiveKit, and viewer simulation. Workbench MUST call only canonical `/api/v1` REST and WebSocket contracts, MUST NOT be copied into the backend container, and MUST NOT be publicly deployed with production.

#### Scenario: Run Workbench locally
- **WHEN** a developer starts Workbench
- **THEN** Vite serves the single console and its TypeScript modules call the configured canonical backend while media is consumed directly from LiveKit

#### Scenario: Build Workbench in CI
- **WHEN** Workbench changes
- **THEN** CI runs formatting, lint, typecheck, unit tests, static build, and Playwright without creating an always-on Workbench service

#### Scenario: Inspect production artifacts
- **WHEN** the backend image and production route table are inspected
- **THEN** they contain neither Workbench assets nor `/lite/*`, `/api/v1/debug/*`, mock, or sandbox-test routes

### Requirement: Explicit local Workbench token fixtures
`workbench/src/dev_tokens.ts` SHALL prefill the current public local viewer and admin token fixtures as `LOCAL_VIEWER_TOKEN_FIXTURE` and `LOCAL_ADMIN_TOKEN_FIXTURE` for fast local testing. The token literals MUST appear only in that source file. The local runtime matrix SHALL use the matching values when backend authentication is enabled. Backend startup MUST reject either known fixture whenever `APP_ENV` is not `dev` or `test`. Workbench MUST keep tokens in page memory, MUST NOT persist them to local storage, and MUST NOT receive provider credentials or LiveKit API secrets.

Gitleaks MAY allowlist these two exact values only for `workbench/src/dev_tokens.ts`, with an explanatory comment. Secret scanning MUST NOT exclude the Workbench directory, disable high-entropy detection, or allow a broader local-token pattern.

#### Scenario: Open the local console
- **WHEN** Workbench starts in local development
- **THEN** viewer and admin inputs are prefilled with the known fixtures and canonical authenticated calls work without manual token entry

#### Scenario: Misconfigure a non-local environment
- **WHEN** stage or production receives either known local fixture as a backend or admin token
- **THEN** backend startup fails before serving requests and identifies the invalid local-only fixture without printing the value

#### Scenario: Scan committed local fixtures
- **WHEN** Gitleaks scans `workbench/src/dev_tokens.ts`
- **THEN** only the two exact documented public fixture values are allowlisted and any other token-like value still fails the secret-scan gate

### Requirement: Complete Workbench fixture datasets
Workbench SHALL own three versioned non-secret datasets under `workbench/src/fixtures/`: `shop_profiles.json`, `viewer_messages.json`, and `products.json`. Shop profiles MUST provide reusable shop, host, persona, and selling-style data. Viewer messages MUST provide categorized normal commerce, product question, purchase-intent, complaint, spam, off-topic, and safety inputs. Products MUST conform to the canonical product request schema. Workbench MUST validate fixture shape before sending it and MUST NOT contain real customer, shop, credential, or provider data.

#### Scenario: Load a complete mock session
- **WHEN** a developer chooses a fixture set
- **THEN** Workbench loads a shop profile, structured products, and categorized viewer messages that can drive the canonical session and platform-ingress flow

#### Scenario: Load an invalid fixture
- **WHEN** a committed or edited fixture does not satisfy its expected shape
- **THEN** Workbench reports the field-level validation failure and sends no partial session configuration

### Requirement: Workbench-owned simulation and smoke behavior
Viewer simulation SHALL run in `workbench/src/simulator.ts` and send deterministic batches through the canonical platform WebSocket. The external smoke client SHALL live at `workbench/scripts/smoke_test.py`, use public REST/WebSocket contracts, and MUST NOT import backend internals. After equivalent behavior tests pass, `core/debug/`, `frontend/lite.html`, the old static entrypoints, and backend debug routes MUST NOT remain in the target tree.

#### Scenario: Simulate viewer traffic
- **WHEN** a developer starts the Workbench simulator
- **THEN** it selects categorized messages from `viewer_messages.json` deterministically and submits them through the same platform-ingress contract used by real integrations

#### Scenario: Execute the Workbench smoke client
- **WHEN** the smoke script runs against a configured backend
- **THEN** it verifies the canonical black-box session flow without importing or mutating backend process state

#### Scenario: Verify the migrated console
- **WHEN** migration reaches parity with the Stage 2 console
- **THEN** Vitest covers reducer, parsing, simulator, fixture, and token-memory behavior and Playwright covers create, configure, ingress, diagnostics, interrupt, and stop before legacy static substring tests are removed

### Requirement: Service observability context and logging ownership
Each product service SHALL provide `observability/context.py` for async-safe `session_id`, `request_id`, `trace_id`, and component context plus `observability/logging/{config,setup,filters,formatter,daily_handler,active_session_handler}.py`. Services MUST propagate validated context identifiers across supported transports and MUST clear bound context after each request or session operation. Context MUST NOT contain secrets, prompts, or customer payloads. Logging setup MUST be idempotent; configuration MUST fail startup for a level outside `DEBUG|INFO|WARNING|ERROR`; filtering MUST allow approved structured fields and redact sensitive-key values; free-form prompts, viewer messages, shop profiles, provider bodies, and credentials MUST be omitted rather than relying on generic PII detection. Product services MUST NOT share a runtime observability package or dependency lock. Platform runtimes SHALL emit stdout/stderr for an external runner or collector and MUST NOT receive project Python observability source.

#### Scenario: Concurrent requests write logs
- **WHEN** two async requests with different context identifiers execute concurrently in one service process
- **THEN** every emitted record contains only the identifiers bound to its own request

#### Scenario: Propagate context to another service
- **WHEN** backend calls a product or platform service through HTTP, WebSocket, SSE, or gRPC
- **THEN** supported transport metadata carries the validated correlation identifiers and the receiving adapter binds them for its operation

#### Scenario: Finish a request
- **WHEN** request handling succeeds or fails
- **THEN** the service clears its bound log context without leaking values into subsequent work

#### Scenario: Configure an unsupported level
- **WHEN** a product service starts with a log level other than `DEBUG`, `INFO`, `WARNING`, or `ERROR`
- **THEN** validated logging configuration fails startup instead of silently selecting another level

#### Scenario: Emit a sensitive structured field
- **WHEN** application code attempts to log a field whose key identifies a token, API key, secret, password, cookie, credential, or provider session token
- **THEN** the filter emits only the redaction marker and never the original value

### Requirement: Active-session and daily log views
Runtime file logs SHALL be grouped under `.runtime/logs/{active-sessions,daily}/{product,platform}/`. Active-session files SHALL use short service identifiers without the `_service` suffix, SHALL truncate when a new session begins, and SHALL remain after the session ends. Daily files SHALL append by service and UTC date and SHALL remove files older than the configured `LOG_RETENTION_DAYS`.

#### Scenario: Start a new product session
- **WHEN** a new session starts after a previous session used `active-sessions/product/backend.log`
- **THEN** the backend active log is truncated before recording the new session while the previous events remain available in the daily backend log

#### Scenario: Record a platform-related session event
- **WHEN** an adapter or collector observes a LiveKit, LMCache, Postgres, or Redis event associated with the current session
- **THEN** it writes the normalized event to the corresponding `active-sessions/platform/<service>.log` without requiring the upstream runtime to understand the application session

#### Scenario: Enforce daily retention
- **WHEN** daily rotation runs at 00:00 UTC
- **THEN** each service starts or appends to `daily/<group>/<service>/YYYY-MM-DD.log` and files older than `LOG_RETENTION_DAYS` are removed

### Requirement: Compact aligned and safe log format
Human-readable logs SHALL use `DD-MM-YYTHH:mm:ssZ | LEVEL | SERVICE: fields` logfmt with a 7-character level column and an 8-character service column. Supported levels SHALL be `DEBUG`, `INFO`, `WARNING`, and `ERROR`. Values containing whitespace MUST be quoted. ANSI color MUST be limited to TTY console output and MUST NOT appear in files. Secret or credential values MUST NOT be emitted.

#### Scenario: Align different levels and services
- **WHEN** records use different supported levels and service identifiers
- **THEN** the level separator and service colon remain vertically aligned

#### Scenario: Write a file log
- **WHEN** a formatted record is written to an active-session or daily file
- **THEN** the record is logfmt text without ANSI escape sequences or sensitive values

### Requirement: Backend-owned Director prompt bundle
Backend Director SHALL live under `backend/application/director/` and SHALL own `base_sales_vi.md`, `director_decision_vi.md`, `response_guardrails_vi.md`, and `fallback_response_vi.md` under `backend/application/director/prompts/`. The loader MUST validate and cache this fixed bundle, MUST reject arbitrary prompt paths, and MUST treat runtime context as untrusted data that cannot override response guardrails. The generic LLM service MUST NOT own Director business prompts.

#### Scenario: Prepare a Director decision
- **WHEN** Director requests the next action for a session
- **THEN** the loader composes the base sales prompt, response guardrails, decision instructions, and delimited runtime context in that order

#### Scenario: Prepare a fallback response
- **WHEN** required context is missing or model output is unavailable or invalid
- **THEN** the loader composes the base sales prompt, response guardrails, fallback instructions, and available delimited runtime context

#### Scenario: Reject an arbitrary prompt path
- **WHEN** configuration or input references a file outside the fixed Director prompt bundle
- **THEN** the loader rejects it before reading or rendering the file

#### Scenario: Record prompt diagnostics
- **WHEN** Director invokes the LLM
- **THEN** logs contain bundle identity, revision or hash, and token counts without rendered prompt text or customer data

### Requirement: Single backend entrypoint and composition root
The backend SHALL expose `services/product/backend_service/src/backend/main.py` as its only server entrypoint. Application construction, typed dependency wiring, and resource lifecycle MUST live under `backend/bootstrap/{app_factory,container,lifespan}.py`. The composition container MUST construct or reference resources without owning their business behavior, and the superseded `server.py` MUST be removed after launch references migrate.

#### Scenario: Import the production application
- **WHEN** an ASGI server imports `backend.main:app`
- **THEN** `main.py` obtains the application from `bootstrap.create_app()` without constructing engines, stores, clients, or Director workers itself

#### Scenario: Build an isolated test application
- **WHEN** a test invokes the application factory with controlled dependencies
- **THEN** it receives an isolated FastAPI application without relying on mutable module-global API dependency state or loading unrelated production resources

#### Scenario: Complete application shutdown
- **WHEN** the ASGI lifespan exits normally, fails, or is cancelled
- **THEN** the lifespan owner performs bounded cleanup of initialized coordinators, publishers, clients, render resources, and database connections and reports cleanup failures with request-independent context

### Requirement: Shared API transport infrastructure
ASGI middleware SHALL live in `backend/api/middleware/{access_log,body_limit,security_headers}.py`; HTTP/WS security SHALL live in `backend/api/security/{authentication,authorization,rate_limit}.py`; exception handlers and shared transport dependencies SHALL live directly under `backend/api/`. `access_log.py` MUST bind safe correlation context, emit method, path, status, and latency, and clear the context on success or failure. `body_limit.py` MUST reject oversized fixed-length and streamed bodies before route handling. CORS MUST use the framework middleware registered by `bootstrap/app_factory.py` rather than a custom wrapper. Authentication MUST precede authorization, preserve the `401` versus `403` distinction, and validate WebSocket credentials before acceptance. Rate limiting MUST cover configured REST, WebSocket, and session scopes. Sensitive values MUST be omitted or redacted by `observability/logging/filters.py` rather than a separate token-redaction transport module.

#### Scenario: Record safe HTTP access
- **WHEN** a versioned HTTP request succeeds or fails
- **THEN** the access log contains safe correlation fields, method, path, status, and latency and its bound context is cleared in a `finally` path

#### Scenario: Reject an oversized declared body
- **WHEN** an HTTP request declares a body larger than the configured byte limit
- **THEN** middleware returns `413` before invoking the matching route

#### Scenario: Reject an oversized streamed body
- **WHEN** a chunked or streamed HTTP body crosses the configured byte limit
- **THEN** middleware stops forwarding the body and returns `413` before route business logic executes

#### Scenario: Apply CORS without a custom module
- **WHEN** the application factory creates the FastAPI application
- **THEN** it registers the installed framework CORS middleware from validated configuration and no `api/middleware/cors.py` exists

#### Scenario: Authorize different API roles
- **WHEN** an unauthenticated caller accesses a protected resource or an authenticated viewer accesses an admin-only resource
- **THEN** authentication returns `401` for the first request and authorization returns `403` for the second

#### Scenario: Reject an unauthorized WebSocket
- **WHEN** a WebSocket presents missing or invalid credentials
- **THEN** authentication rejects it before `accept()` and neither application session state nor platform ingress is exposed

#### Scenario: Limit transport activity
- **WHEN** a caller exceeds its configured REST, WebSocket connection/message, or session activity limit
- **THEN** the shared rate limiter rejects the excess activity without weakening authentication or leaking credentials into logs

### Requirement: Uniform unversioned operational health
Backend, LLM, TTS, and avatar SHALL each own `api/health.py` outside `api/v1` and expose `GET /health/live` plus `GET /health/ready`. Liveness MUST check only process/event-loop survival and MUST NOT call external dependencies. Readiness MUST check only resources required by the active service configuration. Operational health MUST be excluded from `contracts/v1/openapi.json` and MUST NOT expose credentials, stack traces, endpoint secrets, or detailed provider failures. Detailed backend operator diagnostics MAY live behind authenticated v1 admin authorization but MUST NOT create a duplicate versioned health resource.

#### Scenario: Probe liveness during dependency failure
- **WHEN** a required database, model engine, provider, or LiveKit dependency is unavailable but the service process remains responsive
- **THEN** `/health/live` remains successful while `/health/ready` reports not ready without exposing internal failure details

#### Scenario: Generate a v1 contract
- **WHEN** a product service regenerates `contracts/v1/openapi.json`
- **THEN** neither `/health/live` nor `/health/ready` appears in the versioned product contract

### Requirement: Version-owned API schemas and resource routes
API v1 SHALL aggregate route modules in `backend/api/v1/router.py`. The route set MUST be `sessions.py`, `avatars.py`, `voices.py`, `admin.py`, and `websockets.py`; matching public DTO modules MUST live under `backend/api/v1/schemas/` together with `common.py`. Route handlers MUST limit themselves to transport validation, application or client invocation, and response mapping. The backend MUST NOT add versioned `health.py`, `media.py`, `engines.py`, `control.py`, `llm.py`, or `tts.py` production routes.

#### Scenario: Add or modify a v1 request contract
- **WHEN** a sessions, avatars, voices, admin, or WebSocket public payload changes
- **THEN** its DTO is defined in the matching `api/v1/schemas/` module, referenced by the matching resource route, and exported to the owning service's generated v1 contract artifact

#### Scenario: Expose avatar and voice resources
- **WHEN** a user or workbench lists availability, selects an item, or changes allowed avatar or voice configuration
- **THEN** the corresponding resource route validates the public contract and delegates to the owning outbound client without embedding provider behavior

#### Scenario: Keep LLM internal
- **WHEN** Director or playback requires LLM generation
- **THEN** backend invokes the configured LLM client internally and exposes no public LLM or engine-switching route

### Requirement: Flat backend application orchestration
Backend application behavior SHALL remain in one backend process under `backend/application/`, not as nested microservices. Session lifecycle MUST live in `sessions.py`; LLM-to-avatar playback orchestration MUST live in `playback_worker.py`; backpressure and cancellation MUST live in `playback_queue.py`; streaming text boundaries MUST live in `text_chunker.py`; and Director MUST live under `application/director/`. Director MUST preserve explicit modules for coordinator sequencing, session context, business state, decision, decision preparation, comment buffering, clustering, embeddings, scoring, routing, catalog, hooks, events, and prompts rather than leaving those responsibilities inside a coordinator god file or deleting them from the target tree. Event handling MUST invoke injected diagnostics/persistence adapters, and playback execution MUST remain outside Director. A route that only delegates to one outbound client MUST NOT gain an empty application-service wrapper. A global `utils/` package MUST NOT be created without a demonstrated cross-cutting pure helper.

#### Scenario: Execute a session request
- **WHEN** a validated sessions route starts, updates, or ends a session
- **THEN** it invokes `application/sessions.py`, which coordinates the required clients and persistence without importing FastAPI

#### Scenario: Run playback
- **WHEN** a session produces content for playback
- **THEN** the playback worker coordinates LLM, text chunking, TTS, and avatar clients while the queue enforces backpressure and cancellation

#### Scenario: Prepare a Director turn
- **WHEN** the coordinator needs another prepared turn
- **THEN** it delegates state projection and model preparation to the session-context, decision, and decision-preparation modules and does not perform playback or persistence inline

#### Scenario: Complete a Director turn
- **WHEN** playback succeeds, fails, or is cancelled
- **THEN** the event adapter emits safe diagnostics and invokes the configured persistence boundary without adding database or transport behavior to the decision policy

#### Scenario: Proxy a resource without empty layers
- **WHEN** an avatar or voice route requires only one outbound client call and response mapping
- **THEN** it calls that client directly and no pass-through `application/avatars.py` or `application/voices.py` is introduced

### Requirement: Outbound client and service engine ownership
Backend outbound transports SHALL live under `backend/application/clients/`. LLM SHALL use `llm/openai_compatible.py`; TTS SHALL use `tts/self_hosted.py`, `tts/elevenlabs.py`, or `tts/openai_speech.py`; avatar SHALL use `avatar/self_hosted.py`, `avatar/liveavatar.py`, or `avatar/baidu_xiling.py`; and LiveKit control calls SHALL use `livekit.py`. A client MUST own only serialization, server-side authentication, network I/O, bounded timeout/retry, response parsing, and typed transport errors. A hosted-provider client MAY implement the provider's protocol, but MUST NOT contain a model-engine implementation or import API, Director, or playback modules.

`llm_service` SHALL own only the self-host vLLM, SGLang, and Transformers engines plus LMCache integration. `tts_service` SHALL own only the self-host VieNeu and CosyVoice engines. `avatar_service` SHALL own only AvatarForcing, avatar session runtime, and LiveKit publishing. When backend selects a hosted-provider adapter, the corresponding self-host service MUST be undeployed or have desired count zero. Provider credentials and provider session tokens MUST remain server-side. Browser media MUST flow from the selected avatar runtime through LiveKit to the browser without audio or video transiting backend.

#### Scenario: Call an OpenAI-compatible endpoint
- **WHEN** backend selects a self-host LLM endpoint or a hosted OpenAI-compatible endpoint
- **THEN** `llm/openai_compatible.py` uses the configured base URL and server-side credential and returns a parsed result or typed transport failure

#### Scenario: Call a proprietary hosted provider directly
- **WHEN** backend selects ElevenLabs, OpenAI Speech, LiveAvatar, or Baidu Xiling
- **THEN** backend calls the matching provider client directly and the corresponding self-host TTS or avatar service is not kept running as a proxy

#### Scenario: Keep engines outside backend
- **WHEN** vLLM, SGLang, Transformers, VieNeu, CosyVoice, or AvatarForcing implementation changes
- **THEN** the implementation changes in its owning self-host service and no model-engine code is added to backend

#### Scenario: Publish media
- **WHEN** self-host avatar output is ready for a live session
- **THEN** `avatar_service` publishes it to LiveKit and the browser consumes it directly without backend carrying audio or video over HTTP or WebSocket

#### Scenario: Protect provider secrets
- **WHEN** a cloud avatar provider returns session or LiveKit connection data
- **THEN** backend returns only browser-safe LiveKit URL and client-token fields after authorization and never returns provider credentials or provider session tokens

### Requirement: Self-host service composition roots
Each product model service SHALL expose one minimal `main.py` containing its application created by `bootstrap/app_factory.py`. Shared startup and shutdown SHALL be bounded in `bootstrap/lifespan.py`. LLM and TTS MUST NOT add a container abstraction while each owns only one active heavyweight engine. Avatar SHALL use a small typed `bootstrap/container.py` for its independently managed engine, session registry, and LiveKit publisher registry. The services MUST NOT add pass-through `inference.py`, `synthesis.py`, or `rendering.py` modules that only delegate from a route to an active engine.

#### Scenario: Start LLM or TTS
- **WHEN** an LLM or TTS process starts
- **THEN** its lifespan creates exactly the configured active engine and its API dependency exposes that resource without a separate container or pass-through service module

#### Scenario: Start avatar resources
- **WHEN** an avatar process starts
- **THEN** its lifespan populates the typed container with engine, session registry, and LiveKit publishing resources and shuts each resource down within bounded time

#### Scenario: Delegate a model request
- **WHEN** an LLM chat or TTS speech route receives a valid request
- **THEN** it resolves `engines/base.py` through API dependencies and calls the active engine without an intermediate inference or synthesis module

#### Scenario: Manage an avatar session
- **WHEN** an avatar session is created, interrupted, stopped, or cleaned up
- **THEN** `sessions.py` coordinates the engine and LiveKit publisher because it owns a real multi-resource lifecycle

### Requirement: Self-host service API boundaries
Each model service SHALL follow the uniform unversioned `api/health.py` contract and provide `api/middleware/{access_log,body_limit,security_headers}.py`, `api/security/{authentication,authorization,rate_limit}.py`, `api/dependencies.py`, and `api/exception_handlers.py`. Authentication MUST verify workload or service identity; authorization MUST enforce inference, synthesis, rendering, or administrative scopes appropriate to the service; and rate limiting MUST protect request and GPU concurrency. Logging redaction SHALL remain in `observability/logging/filters.py`.

LLM API v1 SHALL own chat-completion and model-discovery routes plus `common.py`, `chat.py`, and `models.py` public schemas. TTS API v1 SHALL own speech and voice-discovery routes plus `common.py`, `speech.py`, and `voices.py` public schemas. Avatar API v1 SHALL own avatar and session routes plus `common.py`, `avatars.py`, and `sessions.py` public schemas. Engine contracts SHALL live in each service's `engines/base.py`, with only self-host engine implementations in that package.

#### Scenario: Probe a service
- **WHEN** an orchestrator probes health or readiness
- **THEN** it calls `/health/live` or `/health/ready` rather than a product API v1 resource

#### Scenario: Reject unsafe service traffic
- **WHEN** a request has invalid service identity, insufficient scope, excessive body size, or exceeds request or concurrency limits
- **THEN** middleware and security reject it before heavyweight engine work and the exception handler returns a safe typed error

#### Scenario: Preserve versioned service contracts
- **WHEN** backend calls an LLM, TTS, or avatar self-host service
- **THEN** the request and response conform to the owning service's API v1 schemas and transport failures are mapped to typed backend client errors

### Requirement: Unambiguous runtime and adapter configuration
Self-host runtime selection SHALL use `LLM_ENGINE=vllm|sglang|transformers`, `TTS_ENGINE=vieneu|cosyvoice`, and `AVATAR_ENGINE=avatarforcing`. Backend outbound selection SHALL use `LLM_ADAPTER=openai_compatible`, `TTS_ADAPTER=self_hosted|elevenlabs|openai_speech`, and `AVATAR_ADAPTER=self_hosted|liveavatar|baidu_xiling`, with the corresponding `LLM_BASE_URL`, `TTS_BASE_URL`, and `AVATAR_BASE_URL`. An `*_ENGINE` value MUST name executable self-host implementation code; an `*_ADAPTER` value MUST name a backend outbound client/protocol. Transport mechanisms such as HTTP, SSE, WebSocket, and gRPC MUST be fixed by the selected adapter and versioned contract rather than represented as engine values.

#### Scenario: Select a self-host runtime
- **WHEN** `TTS_ADAPTER=self_hosted` and `TTS_ENGINE=vieneu`
- **THEN** backend calls the configured TTS service base URL and that service loads the VieNeu engine

#### Scenario: Select a hosted runtime
- **WHEN** `AVATAR_ADAPTER=liveavatar`
- **THEN** backend calls the LiveAvatar client, keeps the self-host avatar deployment at zero, and does not require an avatar engine selector for that request path

#### Scenario: Reject ambiguous selectors
- **WHEN** configuration uses `openai_compat` as an engine or uses `remote_http` or `remote_avatar` as a runtime selector
- **THEN** startup validation fails with a safe error identifying the accepted engine and adapter vocabulary

### Requirement: Internal schemas and runtime persistence ownership
Internal run-plan and utterance types SHALL live under `backend/application/schemas/{run_plan,utterance}.py` and MUST NOT depend on API v1 DTOs. Runtime persistence SHALL live under `backend/db/` with `session_store.py`, memory, Redis, and Postgres adapters, and raw runtime SQL under `backend/db/sql/runtime_schema.sql`. Postgres and Redis runtime configuration SHALL remain under `services/platform/`.

#### Scenario: Reuse an internal schema
- **WHEN** Director or playback consumes a run-plan or utterance
- **THEN** it imports the internal type from `application/schemas/` and maps public DTOs at the route boundary

#### Scenario: Select a session store
- **WHEN** bootstrap selects memory, Redis, or Postgres runtime persistence
- **THEN** it constructs the corresponding `db/` adapter behind the session-store contract without moving SQL or database behavior into API routes

#### Scenario: Apply runtime SQL
- **WHEN** the Postgres runtime schema is initialized or migrated
- **THEN** the backend-owned asyncpg SQL is read from `backend/db/sql/runtime_schema.sql` while Postgres deployment configuration remains under `services/platform/postgres/`

### Requirement: WebSocket control and platform ingress
API v1 SHALL keep one `websockets.py` route module with separate `ws_control` and `ws_platform` handlers while they share transport, session context, and authentication. `ws_control` MUST handle authenticated session commands, ping, interrupt, and server-pushed control events. `ws_platform` MUST accept authenticated, rate-limited viewer-chat ingress into Director input. Neither handler SHALL transport audio or video.

#### Scenario: Control a session
- **WHEN** an authenticated client sends a supported control message
- **THEN** `ws_control` validates the message and invokes session or playback orchestration without handling platform chat or media frames

#### Scenario: Receive viewer chat
- **WHEN** authenticated platform chat arrives within its configured rate limit
- **THEN** `ws_platform` validates it and places it in the Director queue or pending store without handling session control commands

#### Scenario: Keep one module until boundaries diverge
- **WHEN** control and platform handlers still share authentication, session context, and WebSocket protocol infrastructure
- **THEN** they remain explicit handlers in one route module and are split only after protocol or authentication divergence or material file growth

### Requirement: Canonical and safe production API surface
The production backend SHALL expose only unversioned operational health plus the approved `/api/v1` sessions, avatars, voices, admin, and WebSocket resource contracts. Debug, mock, sandbox-test, versioned health, media, engine, control, LLM, and TTS routes MUST NOT be mounted in production. `/lite/*` aliases SHALL be removed after workbench callers migrate to the canonical session API. API errors MUST use `{ "error": { "code": "...", "message": "..." } }` without stack traces, internal paths, raw database errors, secrets, or customer payloads.

#### Scenario: Inspect production routes
- **WHEN** the production application route table or OpenAPI document is generated
- **THEN** it contains unversioned operational health plus the approved versioned sessions, avatars, voices, admin, and WebSocket endpoints and none of the prohibited route families

#### Scenario: Retire the legacy lite API
- **WHEN** all workbench calls have migrated to the canonical session endpoints and compatibility tests pass
- **THEN** `/lite/*` routes are removed rather than retained as a second permanent API contract

#### Scenario: Map an API failure
- **WHEN** validation, an expected application error, or an unexpected exception reaches the API boundary
- **THEN** the registered exception handler returns the stable error envelope with the correct status code and records safe correlation context without exposing internal details

### Requirement: Service-owned generated API contract artifacts
Every product service SHALL own its generated versioned API artifacts under that service's `contracts/v1/` directory and SHALL keep source DTOs under `src/<package>/api/v1/schemas/` and executable compatibility checks under `tests/contract/`. Backend SHALL own `openapi.json` plus `websocket/control.schema.json` and `websocket/platform.schema.json`; LLM, TTS, and avatar SHALL each own `openapi.json`. A repository-root `contracts/` registry MUST NOT be created. The unversioned `/health/live` and `/health/ready` routes of every product service MUST NOT become part of a v1 contract artifact.

#### Scenario: Regenerate a service contract
- **WHEN** CI generates the owning service's OpenAPI and WebSocket artifacts from its routes and Pydantic schemas
- **THEN** generation is deterministic and CI fails if the committed artifacts contain an unexplained diff

#### Scenario: Change a consumed service contract
- **WHEN** an LLM, TTS, avatar, or backend contract artifact changes
- **THEN** CI runs the owner contract tests and only the known backend or Workbench consumer checks for that contract

### Requirement: Ownership-aligned test tiers
Each product service SHALL keep `unit/`, `integration/`, and `contract/` suites under its service-local `tests/` directory. Unit tests MUST NOT use a real network, process, cloud provider, or database. Integration tests MAY use an in-process app and ephemeral local Postgres or Redis. Repository-root `tests/e2e/` SHALL contain only cross-service stack behavior, while `tests/sandbox/` SHALL contain explicitly selected real-provider checks. Benchmarks MUST live outside correctness test directories.

#### Scenario: Run inexpensive feature feedback
- **WHEN** a feature commit changes a product service
- **THEN** CI runs only that service's format, lint, typecheck, and unit checks and starts no external provider, GPU, integration, E2E, or sandbox workload

#### Scenario: Validate an integration candidate
- **WHEN** a feature pull request or merge commit is evaluated
- **THEN** CI runs the affected unit, integration, and contract suites with branch coverage and fails below 80 percent before cached container validation

#### Scenario: Select a sandbox suite
- **WHEN** an authorized operator explicitly runs a sandbox test without required credentials
- **THEN** the selected test fails with the missing requirement rather than silently skipping or falling back to a mock

### Requirement: Isolated product dependencies and container build inputs
Every product service SHALL own an independent `pyproject.toml` and `uv.lock`; product services MUST NOT share one uv workspace lock. A repository-root non-package `pyproject.toml` and `uv.lock` MAY contain only cross-service E2E, sandbox, contract-generation, and repository-tool dependencies and MUST NOT be a runtime input to a product image or product dependency resolution. Repository-level `.editorconfig`, `ruff.toml`, and `pyrightconfig.json` SHALL define common source policy without coupling runtime dependency resolution. Container builds SHALL use repository root as their context and each built Dockerfile SHALL have an adjacent `Dockerfile.dockerignore` that admits only the owning service and explicitly required shared inputs.

#### Scenario: Change one service dependency
- **WHEN** TTS, LLM, avatar, or backend changes a dependency
- **THEN** only that service's lock and dependency cache change and the other product services remain independently reproducible

#### Scenario: Build a product image
- **WHEN** CI builds a service Dockerfile from repository root
- **THEN** its Dockerfile-specific ignore file excludes unrelated services, Workbench, infrastructure, tests, documentation, runtime files, caches, weights, and secrets

#### Scenario: Run cross-service tests
- **WHEN** CI installs the repository-root tool environment for E2E, sandbox, or contract generation
- **THEN** it uses the non-package root lock and no product service imports or resolves runtime dependencies from that environment

### Requirement: Local-only platform composition
The repository root SHALL own one `compose.yaml` for local platform dependencies. A `data` profile SHALL start official Postgres and Redis containers, and a `media` profile SHALL start the pinned local LiveKit wrapper. The Compose file MUST NOT model staging/production infrastructure, contain cloud credentials, or start GPU LLM/TTS/avatar workloads by default.

#### Scenario: Start local data dependencies
- **WHEN** a developer selects the Compose `data` profile
- **THEN** official Postgres and Redis containers start with only local development configuration and their platform smoke checks can verify readiness

#### Scenario: Inspect a non-local deployment
- **WHEN** staging or production is planned or deployed
- **THEN** Terraform and service workflows provide the topology and no Compose definition participates

### Requirement: Repository support files follow real ownership
The monorepo SHALL use one root `.gitignore` and MUST NOT add per-service `.gitignore` files. Service, Workbench, and infrastructure scripts SHALL remain with their owner; benchmarks SHALL live under `benchmarks/`; and root `scripts/` SHALL contain only genuine cross-repository CI, contract-generation, or model-asset helpers. Deployment behavior SHALL remain canonical in GitHub Actions rather than being duplicated in a root deployment implementation.

#### Scenario: Classify a support script
- **WHEN** a script is migrated or introduced
- **THEN** it is placed with its single service, Workbench, or infrastructure owner unless multiple real owners require the root helper

#### Scenario: Ignore generated output
- **WHEN** Python, Node, coverage, Playwright, runtime-log, model, secret, or Terraform output is generated
- **THEN** the root ignore policy excludes it while service locks, Workbench locks and fixtures, generated contract artifacts, Terraform dependency locks, and reviewed Gitleaks configuration remain tracked

### Requirement: CI does not imply deployment
The system SHALL run the applicable CI gate for feature pushes, pull requests, and merge commits. A successful push, pull request, or merge MUST NOT by itself deploy development, staging, or production.

#### Scenario: Merge feature without deployment
- **WHEN** a feature pull request passes CI and is merged into `develop`
- **THEN** CI runs against the exact merge commit and no environment is deployed

#### Scenario: Merge release without production deployment
- **WHEN** a release pull request passes CI and is merged into `main`
- **THEN** CI runs against the exact merge commit and production remains unchanged

### Requirement: Mandatory secret scanning
CI SHALL run Gitleaks for every push and pull request, SHALL fail the required `secret-scan` gate when a secret or credential is detected, and MUST redact matched secret material from workflow logs. GitHub Push Protection SHALL be enabled as an additional prevention layer when supported, but MUST NOT replace the CI gate.

#### Scenario: Detect a committed credential
- **WHEN** a pushed commit or pull request contains content detected as a secret or credential
- **THEN** the `secret-scan` gate fails and blocks integration

#### Scenario: Protect scanner output
- **WHEN** Gitleaks reports a finding
- **THEN** the workflow exposes remediation metadata without printing the matched secret value

#### Scenario: Native push protection is unavailable
- **WHEN** the repository plan or hosting configuration does not provide GitHub Push Protection
- **THEN** the mandatory Gitleaks CI gate remains active for every push and pull request

### Requirement: Explicit development and staging deployment
Development and staging deployments SHALL be started through an explicit workflow dispatch containing an immutable commit SHA and a validated list of services. The same workflow MUST be invocable by GitHub CLI, GitHub web UI, or GitHub API without changing deployment semantics. Development MUST validate, build, push, record digests, deploy, smoke-test, and service-scope rollback only the selected services. Staging MUST additionally run required E2E verification and record the verified digest as production-release evidence.

#### Scenario: Deploy development from the terminal
- **WHEN** an operator invokes `deploy-dev.yml` through GitHub CLI with an eligible commit SHA and service list
- **THEN** the workflow deploys only the selected services to development without requiring a GitHub web-console action

#### Scenario: Deploy staging from the web interface
- **WHEN** an operator invokes `deploy-staging.yml` through the GitHub Actions Run workflow interface with the same valid inputs
- **THEN** the workflow applies the same validation as the CLI invocation, deploys only selected services, passes smoke and E2E checks, and records each verified digest

#### Scenario: Reject ineligible deployment input
- **WHEN** a dispatch specifies an invalid service, an ineligible branch commit, or a commit without its required CI result
- **THEN** the workflow fails before changing the target environment

### Requirement: Event workflows and reusable workflows have distinct roles
Event entry workflows SHALL own branch, pull-request, dispatch, or tag triggers. Underscore-prefixed reusable workflows SHALL expose `workflow_call` and MUST NOT create an alternative ungoverned deployment path.

#### Scenario: Reuse service CI logic
- **WHEN** `ci.yml` needs to validate one or more services
- **THEN** it calls the appropriate reusable CI workflow while retaining ownership of the GitHub event trigger and required gate

### Requirement: Service-scoped Docker build cache
Container builds SHALL use Docker Buildx with GitHub Actions cache import and `mode=min` cache export (final-layer export; `mode=max` costs 10+ min on heavy dependency layers). Each service MUST use a stable, independent cache scope that does not include a branch name or commit SHA. Pull-request verification MUST NOT push an image, but SHALL export its per-service cache (`export_cache: true`) so later develop/main merge builds reuse the layers; untrusted fork runs MUST NOT write to the shared cache. Changes that affect no product service (docs-only, `services_json == '[]'`) SHALL skip `container-build` entirely. Affected-area detection SHALL NOT fail when `github.event.before` has no parent commit (first push of a branch).

#### Scenario: Reuse a service cache
- **WHEN** a trusted build runs again for a service whose unchanged layers were previously cached
- **THEN** Buildx imports that service's GitHub Actions cache and reuses valid layers

#### Scenario: Keep service caches isolated
- **WHEN** backend and avatar images build in the same workflow
- **THEN** each build reads and writes only its own service cache scope

#### Scenario: Verify a pull request image
- **WHEN** a pull request runs container verification
- **THEN** the image is built with `push: false`, its per-service cache is exported for downstream merge builds, and an untrusted fork cannot export entries to the shared cache

#### Scenario: Skip container build for a docs-only change
- **WHEN** a pull request or push changes no product service (`services_json == '[]'`)
- **THEN** the `container-build` job is skipped and `CI / gate` remains success

### Requirement: Service-specific production release
Production deployment SHALL be triggered only by an eligible service-specific version tag. The release workflow MUST verify that the tagged commit belongs to `main`, that the tagged service passed staging smoke and E2E verification for that commit, and that the production deployment uses the exact staging-verified image digest. The promotion job MUST target a protected `production` environment and MUST wait for an authorized approval before receiving production credentials or changing production. Self-approval and protection-rule bypass MUST be disabled when supported; if required approval cannot be enforced, production promotion MUST remain disabled.

#### Scenario: Release an eligible service tag
- **WHEN** a tag such as `backend-v1.2.0` targets a `main` commit with matching successful staging evidence
- **THEN** the release workflow waits for protected-environment approval and then deploys the recorded staging image digest for the backend service to production

#### Scenario: Approval is unavailable or rejected
- **WHEN** no authorized reviewer approves the protected production environment or the required approval control is unavailable
- **THEN** the workflow cannot access production credentials and production remains unchanged

#### Scenario: Reject a tag outside main
- **WHEN** a service tag targets a commit that is not contained in `main`
- **THEN** the release workflow fails before changing production

#### Scenario: Reject a service without staging evidence
- **WHEN** a service tag has no matching successful staging record and image digest
- **THEN** the release workflow fails before changing production

### Requirement: Service-scoped rollback evidence
Each environment deployment SHALL record the selected commit, service, previous image digest, new image digest, initiator, and result. A failed smoke verification MUST restore the affected service to its previous digest without rolling back unrelated services.

#### Scenario: Smoke verification fails
- **WHEN** a newly deployed service fails its post-deployment smoke verification
- **THEN** the workflow restores that service's previous digest, leaves other services unchanged, and reports a failed deployment

