## Why

The monorepo needs one predictable integration and release path so that service changes can be merged independently without accidentally deploying development or production environments. Deployment must remain an explicit, auditable action while still being convenient from the developer terminal. The backend also needs one composition root and one versioned transport boundary so the current server and API god files do not remain the foundation for further Stage 2/3 work.

## What Changes

- Standardize the branch flow as `feature/*` -> pull request to `develop` -> release pull request to `main`.
- Run changed-service fast CI for feature pushes; full integration CI for feature pull requests and `develop` merge commits; and release CI for pull requests and merge commits on `main`, without coupling a successful merge to automatic deployment.
- Protect `develop` and `main` with one stable `CI / gate`, required review, resolved conversations, and a conflict-free/up-to-date head before merge; reject direct feature-to-`main` integration.
- Add a mandatory Gitleaks `secret-scan` CI gate for every push and pull request; redact matched values and fail CI when a secret or credential is detected.
- Cache Docker Buildx layers in GitHub Actions with an independent cache scope per service so CI and non-production deployments reuse work without coupling service caches.
- Organize deployable runtimes under `services/product/` for code owned by this project and `services/platform/` for configured upstream runtimes.
- Keep platform runtimes as pinned, validated upstream packaging rather than application source: local LiveKit tooling with LiveKit Cloud as the development/staging/production media runtime, an evidence-gated LMCache integration with no synthetic health fallback, and local-only Postgres/Redis profiles backed by managed RDS/ElastiCache in staging and production.
- Keep LMCache disabled until benchmark evidence justifies it; when enabled, colocate its multiprocess server with vLLM on compatible GPU capacity instead of operating an independent ARM CPU skeleton.
- Keep the LLM, TTS, and avatar product services self-host-only; place hosted-provider clients in the backend control plane and do not run the corresponding self-host service when a hosted provider is selected.
- Separate runtime engine selection from outbound adapter selection: `*_ENGINE` names an actual self-host engine, while `*_ADAPTER` names the backend client/protocol used to reach a self-host service or hosted provider.
- Standardize async-safe observability context and a real logging package in every product service: validated configuration, idempotent setup, structured-field filtering/redaction, aligned human logfmt, TTY-only color, per-service active-session overwrite, and UTC daily retention grouped by product and platform services.
- Move Director system prompts out of Python configuration into a validated Vietnamese prompt bundle owned by the backend and composed separately for decision and fallback flows.
- Replace the backend `server.py` and monolithic `api/v1.py` layout with one minimal `main.py`, an application-level `bootstrap/` composition package, explicit API middleware/security/error handling, one unversioned operational health module shared by convention across product services, version-owned business routes and schemas, flat application orchestration, outbound clients, internal schemas, and colocated database adapters/SQL.
- Decompose DirectorCoordinator without deleting real behavior: retain explicit session context/state, comment buffering, clustering, embeddings, scoring, routing, decision, decision preparation, event/persistence adaptation, and move playback execution into the application playback worker.
- Keep production on the canonical `/api/v1` contract, migrate workbench callers away from `/lite/*`, and exclude debug, mock, and sandbox-test endpoints from the production backend.
- Replace the three-file static frontend with one developer-only `workbench/` built from Vite, vanilla TypeScript, Tailwind CSS, Vitest, and Playwright; split the Stage 2 HTML god file into flat modules without introducing a UI framework or application server.
- Move debug fixtures, viewer-traffic simulation, and external smoke behavior out of backend source into Workbench; retain explicit hardcoded viewer/admin local-token fixtures for fast testing, but reject those known values outside dev/test and narrowly allowlist only those exact public fixtures in secret scanning.
- Give each product service its own unit, integration, and contract test suites; reserve root `tests/e2e/` and `tests/sandbox/` for cross-service behavior, and keep benchmarks outside correctness tests.
- Keep generated API contract artifacts with their owning service under `contracts/v1/`, distinct from source schemas under `src/<package>/api/v1/schemas/` and verification code under `tests/contract/`; do not create a root contract registry.
- Give every product service an independent `pyproject.toml` and `uv.lock`, while sharing only repository-level format and typecheck policy, so incompatible GPU dependency stacks remain independently reproducible.
- Use repository-root container build contexts with Dockerfile-specific ignore files, one root `.gitignore`, and scripts organized by the narrowest real owner: service-local, infrastructure-local, Workbench-local, benchmark, or cross-repository tooling.
- Keep a root tool-only `pyproject.toml` and `uv.lock` for cross-service E2E/sandbox/contract tooling without making product services share runtime dependencies, and add one local-only `compose.yaml` for official Postgres/Redis plus local LiveKit profiles.
- Make development and staging deployment explicit workflow dispatches against a selected commit and selected services.
- Treat "manual deployment" as an explicitly authorized trigger that can be invoked through GitHub CLI by default, or through the GitHub web UI or API when needed.
- Allow production release only from a service-specific version tag whose commit belongs to `main` and has passed staging verification.
- Require staging smoke/E2E verification and a protected production-environment approval before promoting the staging-tested digest.
- Keep CI integration and CD authorization separate so a feature may be integrated without creating runtime cost or changing an environment.
- Keep the existing concern-based Terraform module boundaries, split the `compute/main.tf` god file into shared and service-owned files inside the same module, and add a separate `staging` root/state without Terraform workspaces.
- Run backend, LLM, TTS, and avatar as independent ECS services in one environment cluster; allocate self-host compute only when its backend adapter is selected, keep LMCache colocated and disabled until benchmark evidence exists, and remove the combined LLM/TTS task, standalone LMCache capacity, LiveKit Fargate service, and internal model-service NLB.
- Make Terraform plan part of CI but keep infrastructure apply and non-production teardown as explicit protected workflow dispatches, separate from application-service deployment.
- Pin every deployed image by immutable digest, restrict GitHub OIDC roles per environment, keep application/provider token values out of Terraform state, and migrate S3 backend locking from deprecated DynamoDB locking to native lockfiles.

## Capabilities

### New Capabilities

- `branch-governed-service-delivery`: Defines the service source and backend API layout, observability and Director prompt contracts, branch integration, mandatory secret scanning, service-scoped container build caching, CI gates, explicit development/staging deployment, and service-scoped production release rules for the monorepo.

### Modified Capabilities

None.

## Impact

- Affects GitHub Actions workflows under `.github/workflows/` and developer-facing deployment commands or wrappers under `scripts/`.
- Adds Gitleaks to CI and enables GitHub Push Protection when the repository entitlement supports it.
- Configures Docker Buildx GitHub Actions cache in the shared container-build workflow, isolated by service.
- Moves project-owned services to `services/product/{backend_service,llm_service,tts_service,avatar_service}` and upstream runtime configuration to `services/platform/{livekit,lmcache,postgres,redis}`.
- Retains pinned local LiveKit packaging and validation while using LiveKit Cloud for development/staging/production; removes the AWS LiveKit Fargate Spot topology and keeps a dedicated VM-based self-host deployment as a future evidence-driven decision.
- Pins the LMCache upstream runtime, removes best-effort installs and fake-success fallbacks, and keeps LMCache disabled and colocated with LLM capacity until a measured cache benefit is demonstrated.
- Uses managed RDS and ElastiCache for staging/production while retaining only local runtime documentation, configuration, and smoke checks under the Postgres and Redis platform directories; backend-owned SQL remains under `backend/db/sql/`.
- Adds per-product-service `observability/context.py` plus `observability/logging/{config,setup,filters,formatter,daily_handler,active_session_handler}.py`, with runtime logs under `.runtime/logs/{active-sessions,daily}/{product,platform}/`.
- Adds `services/product/backend_service/src/backend/application/director/prompts/` with four responsibility-specific prompt files and a safe cached loader.
- Establishes `services/product/backend_service/src/backend/main.py` as the only server entrypoint and moves application construction, dependency wiring, and lifecycle management into `backend/bootstrap/`.
- Splits the backend transport adapter into unversioned `api/health.py`, shared `api/{middleware,security}`, and `api/v1/{routes,schemas}` for sessions, avatars, voices, admin, and WebSocket business resources, with consistent exception mapping and no production debug/mock, media, engine, control, LLM, or TTS route surface.
- Uses `/health/live` for dependency-free process liveness and `/health/ready` for configured dependency readiness in backend, LLM, TTS, and avatar; operational health is excluded from `contracts/v1/openapi.json`, while detailed operator diagnostics remain authenticated under the backend admin resource.
- Places session/playback orchestration, Director, thin outbound LLM/TTS/avatar/LiveKit clients, and internal run-plan/utterance schemas under `backend/application/`; these clients may implement hosted-provider protocols but do not contain model-engine implementations.
- Colocates runtime persistence adapters and raw runtime SQL under `backend/db/`, while Postgres and Redis runtime configuration remains under `services/platform/`.
- Keeps self-host LLM engines in `llm_service`, self-host TTS engines in `tts_service`, and the self-host avatar engine, session runtime, and LiveKit publishing in `avatar_service`; hosted-provider calls originate from backend clients and browser media continues to flow directly from LiveKit.
- Allows an unneeded self-host model service to remain undeployed or at desired count zero when the backend selects a hosted-provider adapter, avoiding a paid proxy runtime with no model workload.
- Establishes required checks and release eligibility for `feature/*`, `develop`, `main`, and service-specific tags.
- Adds repository-aware change detection so service CI, workbench checks, platform validation, Terraform checks, and cached container validation run only for affected areas while `CI / gate` remains stable.
- Adds service-owned contract artifacts and drift checks, service-local unit/integration/contract tests, root cross-service E2E and opt-in sandbox tests, branch coverage enforcement, and consumer-specific CI fan-out when a service contract changes.
- Replaces the shared runtime dependency lock with independent product-service locks, retains a non-package root tooling lock for cross-service checks only, adds a local-only Compose launcher, and gives each Dockerfile an explicit `Dockerfile.dockerignore` for the repository-root build context.
- Separates correctness tests from `benchmarks/`, moves infrastructure operations under `infra/scripts/`, keeps only genuine cross-repository helpers under root `scripts/`, and uses one root `.gitignore` for generated outputs across the monorepo.
- Renames `frontend/` to `workbench/`, consolidates `index.html`, `lite.html`, and `stage2.html` into one modular console, and adds versioned shop-profile, viewer-message, and product fixture datasets.
- Removes `core/debug/` and `/api/v1/debug/*` after their required fixture, simulator, and smoke capabilities use canonical REST/WebSocket contracts from Workbench; Workbench is excluded from backend images and public production deployment.
- Does not deploy any environment merely because code was merged.
- Keeps Terraform cloud provisioning under `infra/` rather than mixing it with service runtime configuration.
- Adds an explicit `infra/environments/staging/` root and independent `global`, `dev`, `staging`, and `prod` state keys.
- Preserves the concern-based Terraform modules while splitting compute resources into `cluster.tf`, `iam.tf`, `discovery.tf`, `backend.tf`, `llm.tf`, `tts.tf`, and `avatar.tf` inside `infra/modules/compute/`.
- Uses one ECS cluster per environment with independently deployable product services, direct Cloud Map private discovery for internal model APIs, and only the backend behind the public ALB.
- Defaults dev data and GPU capacity to off, uses on-demand backend capacity in staging/production, and initially accepts single-AZ RDS and single-node Redis as an explicit MVP cost trade-off.
- Adds protected manual infrastructure apply and non-production teardown workflows; application deployment updates task definitions and services but never performs Terraform apply.
