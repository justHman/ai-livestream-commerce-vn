## Context

The repository already has CI and deployment workflows, but their responsibilities are not yet governed by one explicit branch-to-environment contract. The target workflow must support independently changing services in a monorepo while allowing a feature to be integrated without deploying or incurring runtime cost.

The repository uses `feature/*` for feature work, `develop` for integration, and `main` for releasable code. Development and staging deployments are operator-authorized actions. Production is released per service from an eligible version tag.

Deployable units also need one ownership boundary: project-owned product code and upstream runtime configuration currently participate in the same system but require different maintenance and security treatment.

Inside the backend service, `core/server.py` currently combines process entry, app construction, dependency wiring, resource lifecycle, middleware, and error handling, while `core/api/v1.py` combines transport schemas, domain routes, WebSockets, dependency state, and test-only endpoints. The service boundary is valid, but these internal responsibilities need explicit ownership before Stage 2/3 expansion.

## Goals / Non-Goals

**Goals:**

- Give every branch event one unambiguous CI/CD outcome.
- Block integration when CI detects committed secrets or credentials without exposing the matched value in logs.
- Reuse Docker build layers across trusted CI and deployment runs without allowing one service to overwrite another service's cache.
- Make project-owned product services and configured upstream platform runtimes immediately distinguishable by path.
- Keep logs consistent, compact, async-safe, retention-bounded, and free of secrets across independently deployed services.
- Keep Director behavior reviewable, testable, and rollback-friendly without coupling business prompts to the generic LLM service.
- Give the backend one minimal entrypoint and one testable application composition root.
- Keep application bootstrap separate from the HTTP/WS transport adapter, while keeping middleware, security, errors, routes, and versioned schemas in predictable API packages.
- Remove debug, mock, sandbox-test, and migrated legacy routes from the production API surface.
- Keep code integration separate from environment deployment.
- Make explicit deployments convenient from the terminal and auditable in GitHub Actions.
- Deploy only a selected commit and selected services.
- Ensure a production tag refers to a `main` commit that passed staging verification.
- Reuse the staging-tested image digest for production instead of rebuilding different bytes.

**Non-Goals:**

- Automatically deploy after merging into `develop` or `main`.
- Introduce new business endpoint semantics or change Terraform module boundaries beyond the canonical API migration described here.

## Decisions

### 1. Use a branch-governed integration path

The canonical path is:

```text
feature/* -> pull request to develop -> release pull request to main
```

Direct integration outside this path is prevented through branch protection and required CI checks. Feature pushes provide early feedback; pull requests and merge commits run the applicable integration gate. A merge never implies deployment.

This was chosen over direct feature-to-main releases because `develop` provides a stable integration point for cross-service changes without making every accepted feature production-releasable immediately.

Repository rules for both `develop` and `main` require a pull request, the stable `CI / gate` status, at least one approving review, resolution of all review conversations, and a conflict-free head that is current with the target branch. Direct pushes are denied except through an explicitly audited emergency bypass. `main` accepts the release path from `develop`; a `feature/*` branch does not integrate directly into `main`.

### 2. Separate event workflows from reusable workflow building blocks

Human- and Git-event entry workflows use descriptive names without a leading underscore:

- `ci.yml`: CI entry point for pushes and pull requests.
- `deploy-dev.yml`: explicit development deployment.
- `deploy-staging.yml`: explicit staging deployment.
- `release-service.yml`: production release from a service-specific tag.

Reusable implementation workflows use a leading underscore and `workflow_call`, for example `_python-service-ci.yml`, `_container-build.yml`, and `_deploy-service.yml`. They are called by entry workflows and are not a separate release path.

This avoids duplicating service matrices, build steps, and deployment logic while keeping the Actions screen understandable.

`ci.yml` owns one mandatory `secret-scan` job that runs Gitleaks for every push and pull request, including merge commits because they are push events. The job fails the stable CI gate on a finding and uses redacted output so matched secret material never appears in Actions logs. GitHub Secret Scanning and Push Protection are enabled as an additional prevention layer when the repository entitlement supports them; they do not replace the portable Gitleaks gate.

`_container-build.yml` owns Docker Buildx configuration. It uses `cache-from: type=gha` and `cache-to: type=gha,mode=max` with `scope=<service>` so backend, LLM, TTS, and avatar builds do not overwrite each other's cache. Branch names and commit SHAs are not part of the scope because BuildKit already invalidates changed layers and those values would prevent useful reuse. Pull-request verification builds use `push: false`; trusted development and staging deployments use `push: true`. Untrusted fork runs do not write shared caches. Production promotes the staging-tested digest and never rebuilds.

### 3. Define "manual deployment" as an explicit trigger, not a required web click

Development and staging workflows expose `workflow_dispatch` inputs for an immutable commit SHA and a service list. The default developer interface is GitHub CLI:

```text
gh workflow run deploy-dev.yml --ref develop -f commit_sha=<sha> -f services=backend_service,tts_service
gh workflow run deploy-staging.yml --ref main -f commit_sha=<sha> -f services=backend_service,tts_service
```

The GitHub Actions **Run workflow** button and GitHub REST API may invoke the same workflow. They do not define different deployment behavior. A repository script may wrap the CLI command later, but the workflow remains the source of truth.

The selected workflow validates that the commit belongs to the required branch and has passed the required CI gate before changing an environment.

### 4. Use this event-to-action matrix

| Event | Required result | Deployment |
|---|---|---|
| Push to `feature/*` | Fast CI feedback | None |
| Pull request from `feature/*` to `develop` | Full integration CI | None |
| Merge into `develop` | CI on the exact merge commit | None |
| Pull request from `develop` to `main` | Full release CI | None |
| Merge into `main` | CI on the exact merge commit | None |
| Dispatch `deploy-dev.yml` | Validate ref and deploy selected services | Development only |
| Dispatch `deploy-staging.yml` | Validate ref, deploy selected services, record verified digests | Staging only |
| Push eligible service tag | Validate release evidence and deploy one service | Production only |

`ci.yml` always starts for the governed push and pull-request events so branch protection never waits on a path-skipped required workflow. Its first jobs run the mandatory secret scan and compute a repository-aware affected-area map. Changes to service-owned contract artifacts, shared configuration, dependency locks, or CI/build infrastructure fan out to the affected consumers rather than being treated as belonging only to the changed file's service.

The event modes are:

| Event mode | Required jobs |
|---|---|
| Feature push | Secret scan, affected-area detection, format, lint, typecheck, and unit tests for changed product services only |
| Feature PR to `develop` | Secret scan; format, lint, typecheck, unit, integration, contract, and coverage gates for affected services; cached affected-image build with `push: false`; workbench checks when workbench changes; platform validation when platform changes; Terraform format/validate/plan when infrastructure changes |
| Merge push to `develop` | Re-run full affected-area integration CI and cached affected-image validation with `push: false` against the exact merge commit |
| Release PR from `develop` to `main` | Repository-aware release CI with affected service tests, container validation, workbench/platform checks when affected, and Terraform plan when infrastructure changes |
| Merge push to `main` | Re-run release verification against the exact merge commit without pushing an image or deploying an environment |

Feature-push mode deliberately omits integration tests, contract tests, coverage enforcement, full container builds, and every deployment job to provide inexpensive feedback while code is still changing. Pull-request and merge modes perform the more expensive checks because those commits are candidates for shared integration or release. Every mode ends in one stable `CI / gate`; skipped unaffected-area jobs report a successful neutral result to that gate rather than creating branch-protection deadlocks.

### 5. Release production per service from verified immutable artifacts

Production tags identify one service and version, for example `backend-v1.2.0` or `avatar-v0.5.0`. `release-service.yml` parses the tag, verifies that its commit is contained in `main`, verifies successful staging smoke and E2E evidence for that service and commit, and resolves the exact recorded image digest. The deployment job then targets a protected `production` environment and waits for an authorized approval before it can access production credentials or promote the digest. Self-approval and administrator bypass are disabled where the repository entitlement supports those controls; production remains blocked rather than silently bypassing approval when the required protection cannot be enforced.

This was chosen over rebuilding on the production tag because digest promotion prevents dependency or build-time drift between staging and production.

### 6. Keep rollback service-scoped

Each deployment records the previous and new image digest. If smoke verification fails, the deployment workflow restores only the affected service to its previous digest and reports failure. Other services remain unchanged.

### 7. Separate product services from platform runtimes

All independently deployable runtime definitions live under `services/` with one classification layer:

```text
services/
├── product/
│   ├── backend_service/
│   ├── llm_service/
│   ├── tts_service/
│   └── avatar_service/
└── platform/
    ├── livekit/
    ├── lmcache/
    ├── postgres/
    └── redis/
```

Each `services/product/*_service/` directory owns that service's source, package metadata, container definition, and service-local scripts. Its import package remains concise after `src/`, for example `services/product/llm_service/src/llm/`.

Each `services/platform/*/` directory contains only the configuration and deployment assets needed to operate its upstream runtime. Upstream source is not copied into this repository. Terraform cloud resources remain in `infra/`; service runtime configuration is not duplicated there.

### 8. Use async-safe context and two service-grouped log views

Each product service owns these source modules under its existing source package:

```text
observability/
├── __init__.py
├── context.py
└── logging/
    ├── __init__.py
    ├── config.py
    ├── setup.py
    ├── filters.py
    ├── formatter.py
    ├── daily_handler.py
    └── active_session_handler.py
```

`context.py` owns `ContextVar` values for `session_id`, `request_id`, `trace_id`, and `component`. Inbound middleware binds validated identifiers, outbound clients propagate them through the selected transport metadata, and request/session cleanup clears them in `finally`. Context fields never contain tokens, credentials, prompts, or customer payloads. This prevents async requests from leaking context into each other's logs and keeps the same contract when services scale independently.

The logging package is deliberately split because configuration, record filtering, formatting, active-session overwrite, and daily retention have independent behavior and failure modes; combining them would create a new logging god file. `config.py` validates `DEBUG|INFO|WARNING|ERROR`, service identity, runtime root, retention, and TTY color policy. `setup.py` performs idempotent process logging setup. `filters.py` injects approved context fields and redacts sensitive-key values; free-form prompts, viewer messages, shop profiles, provider bodies, and credentials are omitted rather than heuristically scrubbed. `formatter.py` emits aligned human logfmt and adds ANSI color only for a TTY console. `daily_handler.py` owns UTC date rotation and retention. `active_session_handler.py` owns one service file that truncates at the start of a new session and remains after completion; the explicit name avoids implying one file per `session_id`.

The default runtime root is `.runtime/logs/` and can be relocated through validated configuration:

```text
.runtime/logs/
├── active-sessions/
│   ├── product/
│   │   ├── backend.log
│   │   ├── llm.log
│   │   ├── tts.log
│   │   └── avatar.log
│   └── platform/
│       ├── livekit.log
│       ├── lmcache.log
│       ├── postgres.log
│       └── redis.log
└── daily/
    ├── product/{backend,llm,tts,avatar}/YYYY-MM-DD.log
    └── platform/{livekit,lmcache,postgres,redis}/YYYY-MM-DD.log
```

At the start of a new session, every participating service's active-session view is opened with truncate semantics and then retained when the session ends. It therefore represents only the latest session for that service. Daily files append, rotate at 00:00 UTC, and retain `LOG_RETENTION_DAYS` days. Platform active-session files contain normalized platform-related events written by project adapters or the log collector because upstream runtimes such as Postgres and Redis do not understand application session identifiers; their complete operational history remains in daily platform logs.

The human logfmt representation is:

```text
01-08-26T03:12:44Z | INFO    | backend : evt=session_started sid=abc
01-08-26T03:12:45Z | WARNING | postgres: evt=query_slow latency_ms=420
```

Levels are exactly `DEBUG`, `INFO`, `WARNING`, and `ERROR`; another configured value fails startup rather than silently falling back. The level column is left-aligned to 7 characters and the service column to 8, which aligns the current longest service name `postgres`. Console output uses color only when attached to a TTY. Files contain no ANSI escapes. Stable short fields include `evt`, `sid`, `rid`, `trace_id`, `cmp`, `provider`, `latency_ms`, and `error`; values containing whitespace are quoted and sensitive values are redacted or omitted.

Product services own this Python package independently; no shared runtime observability library couples their dependency locks. Upstream platform containers continue to emit stdout/stderr. A local runner or production log collector normalizes those streams into platform views instead of copying project Python source into LiveKit, LMCache, Postgres, or Redis.

Each replica writes to its own local runtime path or stdout stream and never shares one writable log file with another replica. Production aggregation labels the service and instance externally, preserving horizontal scaling without changing the requested filenames inside each isolated runtime.

### 9. Own a composed Vietnamese prompt bundle in Backend Director

Director is backend application logic, and its prompts live at:

```text
services/product/backend_service/src/backend/application/director/
├── __init__.py
└── prompts/
    ├── __init__.py
    ├── loader.py
    ├── base_sales_vi.md
    ├── director_decision_vi.md
    ├── response_guardrails_vi.md
    └── fallback_response_vi.md
```

`base_sales_vi.md` defines persona, language, tone, and sales principles. `director_decision_vi.md` defines the decision task. `response_guardrails_vi.md` contains non-overridable response and safety constraints. `fallback_response_vi.md` defines behavior for missing context or invalid/failed model output.

The decision flow composes base, guardrails, decision instructions, and runtime context. The fallback flow substitutes fallback instructions for decision instructions. Runtime shop, product, comment, and session values are passed as clearly delimited untrusted data and cannot override the static guardrails.

`loader.py` loads only the fixed bundle files, validates their presence, and caches their static contents at process startup. Environment configuration cannot supply an arbitrary prompt path. Logs record only bundle identity, Git revision or content hash, and token counts; rendered prompts and customer data are never logged. The LLM service remains a generic inference service and does not own Director business prompts.

### 10. Separate backend composition from versioned API transport

The backend target layout is:

```text
services/product/backend_service/
├── Dockerfile
├── Dockerfile.dockerignore
├── pyproject.toml
├── uv.lock
├── README.md
├── contracts/
│   └── v1/
│       ├── openapi.json
│       └── websocket/
│           ├── control.schema.json
│           └── platform.schema.json
├── scripts/
│   ├── start.sh
│   ├── migrate.py
│   └── smoke_test.py
└── src/backend/
    ├── __init__.py
    ├── main.py
    ├── config.py
    ├── bootstrap/
    │   ├── __init__.py
    │   ├── app_factory.py
    │   ├── container.py
    │   └── lifespan.py
    ├── api/
    │   ├── __init__.py
    │   ├── dependencies.py
    │   ├── exception_handlers.py
    │   ├── health.py
    │   ├── middleware/
    │   │   ├── __init__.py
    │   │   ├── access_log.py
    │   │   ├── body_limit.py
    │   │   └── security_headers.py
    │   ├── security/
    │   │   ├── __init__.py
    │   │   ├── authentication.py
    │   │   ├── authorization.py
    │   │   └── rate_limit.py
    │   └── v1/
    │       ├── __init__.py
    │       ├── router.py
    │       ├── schemas/
    │       │   ├── __init__.py
    │       │   ├── common.py
    │       │   ├── sessions.py
    │       │   ├── avatars.py
    │       │   ├── voices.py
    │       │   ├── admin.py
    │       │   └── websockets.py
    │       └── routes/
    │           ├── __init__.py
    │           ├── sessions.py
    │           ├── avatars.py
    │           ├── voices.py
    │           ├── admin.py
    │           └── websockets.py
    ├── application/
    │   ├── __init__.py
    │   ├── sessions.py
    │   ├── playback_worker.py
    │   ├── playback_queue.py
    │   ├── text_chunker.py
    │   ├── clients/
    │   │   ├── __init__.py
    │   │   ├── llm/
    │   │   │   ├── __init__.py
    │   │   │   └── openai_compatible.py
    │   │   ├── tts/
    │   │   │   ├── __init__.py
    │   │   │   ├── self_hosted.py
    │   │   │   ├── elevenlabs.py
    │   │   │   └── openai_speech.py
    │   │   ├── avatar/
    │   │   │   ├── __init__.py
    │   │   │   ├── self_hosted.py
    │   │   │   ├── liveavatar.py
    │   │   │   └── baidu_xiling.py
    │   │   └── livekit.py
    │   ├── schemas/
    │   │   ├── __init__.py
    │   │   ├── run_plan.py
    │   │   └── utterance.py
    │   └── director/
    │       ├── __init__.py
    │       ├── config.py
    │       ├── coordinator.py
    │       ├── session_context.py
    │       ├── state.py
    │       ├── decision.py
    │       ├── decision_preparation.py
    │       ├── comment_buffer.py
    │       ├── clustering.py
    │       ├── embeddings.py
    │       ├── scoring.py
    │       ├── routing.py
    │       ├── catalog.py
    │       ├── hooks.py
    │       ├── events.py
    │       └── prompts/
    │           ├── __init__.py
    │           ├── loader.py
    │           ├── base_sales_vi.md
    │           ├── director_decision_vi.md
    │           ├── response_guardrails_vi.md
    │           └── fallback_response_vi.md
    ├── db/
    │   ├── __init__.py
    │   ├── session_store.py
    │   ├── memory_session_store.py
    │   ├── redis_session_store.py
    │   ├── postgres_runtime_store.py
    │   └── sql/
    │       └── runtime_schema.sql
    └── observability/
        ├── __init__.py
        ├── context.py
        └── logging/
            ├── __init__.py
            ├── config.py
            ├── setup.py
            ├── filters.py
            ├── formatter.py
            ├── daily_handler.py
            └── active_session_handler.py
```

`main.py` is the only server entrypoint and contains only `app = create_app()`. The existing `server.py` is removed after launch references migrate. `bootstrap/` remains outside `api/` because it composes the whole process, not one transport version. `app_factory.py` creates FastAPI and registers lifespan, middleware, framework `CORSMiddleware`, exception handlers, and the v1 router. `container.py` is a lightweight typed resource container rather than a dependency-injection framework or mutable service locator. `lifespan.py` performs bounded startup and shutdown of shared resources.

Middleware and HTTP/WS security live under `api/` but outside `api/v1/` because they apply across API versions. `access_log.py` binds safe request/session correlation context, emits method/path/status/latency access records, and always clears context in `finally`. `body_limit.py` rejects oversized fixed-length and streamed/chunked HTTP bodies with `413` before route logic. `security_headers.py` owns response hardening. CORS uses FastAPI/Starlette's installed middleware directly in `app_factory.py`; a custom `cors.py` adds no value.

`authentication.py` verifies bearer and WebSocket credentials, uses constant-time secret comparison where applicable, resolves identity, and rejects invalid WebSockets before `accept()`. `authorization.py` enforces viewer/admin permissions and preserves the `401` unauthenticated versus `403` unauthorized distinction. `rate_limit.py` limits REST requests, WebSocket connections/messages, and session-scoped activity. Logging redaction stays in `observability/logging/filters.py`; no separate API token-redaction module is added, and sensitive values are omitted by default.

`api/v1/schemas/` owns only the versioned public request, response, error, and WebSocket DTOs. `application/schemas/` owns internal run-plan and utterance types. There is no global `backend/schemas/` or ambiguous `models/` package. Route modules perform transport validation and response mapping, then call application orchestration or a thin outbound client; no parallel controllers layer is added.

Every product service registers unversioned `GET /health/live` and `GET /health/ready` handlers from `api/health.py`. Liveness checks only process/event-loop survival and never calls dependencies; readiness checks only dependencies required by the configured adapter/runtime. Health responses expose no secret, internal stack, or detailed provider failure and are excluded from `contracts/v1/openapi.json`. Authenticated detailed runtime diagnostics, when needed, remain under the backend v1 admin resource.

The canonical backend v1 production routes are sessions, avatars, voices, admin, and WebSockets. `sessions.py` owns session lifecycle and LiveKit session metadata. `avatars.py` and `voices.py` expose user-facing listing, selection, configuration, and availability facades. A separate backend LLM route is unnecessary because LLM execution is internal. There are no versioned `health.py`, `media.py`, `engines.py`, `control.py`, `llm.py`, or `tts.py` route modules: operational health is unversioned, the backend itself is the control plane, engine/runtime selection belongs to the owning service, and audio/video flows directly through LiveKit rather than backend HTTP or WebSocket transport.

One `websockets.py` owns two clearly named handlers while they share authentication, session context, and transport: `ws_control` handles ping, interrupt, session commands, and server-pushed control events; `ws_platform` accepts authenticated, rate-limited viewer chat ingress into the Director queue or pending store. Split them only when their protocol/authentication diverges or the module becomes materially large. Neither handler carries audio or video.

`application/` is deliberately flat for orchestration. `sessions.py` owns session lifecycle; `playback_worker.py` coordinates LLM -> chunking -> TTS -> avatar; `playback_queue.py` owns backpressure and cancellation; and `text_chunker.py` owns streaming text boundaries. Queue and chunker remain separate because combining the existing responsibilities would recreate an approximately 900-line god file. A route that only proxies one client does not receive an empty `application/avatars.py` or `application/voices.py` wrapper.

Director decomposition preserves current behavior instead of reducing the package to two misleading files. `coordinator.py` owns tick/lifecycle sequencing only; `session_context.py` owns per-session resources, tasks, queues, cancellation, and generation revision; `state.py` owns the business FSM and run-plan/product cursor; `decision.py` owns reactive/proactive/idle/close choice; `decision_preparation.py` owns prompt composition, model generation, and prepared variants; `comment_buffer.py`, `clustering.py`, `embeddings.py`, `scoring.py`, and `routing.py` retain their distinct stream-analysis responsibilities; `catalog.py` and `hooks.py` own structured commerce inputs; and `events.py` emits diagnostics and invokes the injected persistence adapter. Playback execution remains in the application-level worker so Director chooses what to say without owning LLM-to-avatar media execution.

`application/clients/` contains outbound transport adapters only: request serialization, server-side credentials, bounded timeout/retry, response parsing, and typed transport failures for self-host services, hosted providers, and LiveKit. The OpenAI-compatible LLM client can target either a self-host endpoint or a hosted compatible endpoint by base URL; proprietary TTS and avatar protocols have explicit thin clients. These clients never import API, Director, or playback code and do not contain model-engine implementations. The backend owns Director and session/playback orchestration; `llm_service`, `tts_service`, and `avatar_service` own only self-host runtimes. Provider secrets and provider-side session tokens never reach the browser. Browser-safe LiveKit connection data may be returned after backend authorization, while audio and video still flow directly through LiveKit.

`db/` colocates the runtime persistence port, memory/Redis/Postgres adapters, and their raw asyncpg SQL under `db/sql/`. Postgres and Redis deployment/runtime configuration remains under `services/platform/`. No global `utils/` is created; a helper moves into a shared utility module only after a real cross-cutting pure use appears.

`api/exception_handlers.py` maps validation, typed application, and unexpected errors to the stable `{ "error": { "code": "...", "message": "..." } }` envelope without leaking stack traces or internal details. Workbench callers migrate from `/lite/*` before those aliases are removed. Debug, mock, and sandbox-test routes are never mounted in the production backend.

The dependency direction is:

```text
main.py -> bootstrap/ -> api/ + application/ + db/
api/v1/routes/ -> application/sessions.py or application/clients/
application/sessions.py + application/director/ + playback_* -> application/clients/ + db/
```

These application modules run in one backend process; they are not nested microservices. The deployable microservice boundary is `services/product/*_service`. The API adapter does not construct engines, clients, stores, or Director workers, and application or database code does not import FastAPI transport modules.

### 11. Keep self-host runtimes separate from hosted-provider clients

The model services exist only to operate project-owned self-host workloads. A hosted provider already supplies its own runtime, scaling, and public API, so routing that call through an otherwise idle GPU service would add cost and another failure boundary without adding model ownership. Hosted-provider clients therefore live in the backend control plane. When a hosted provider is selected, the corresponding self-host service is not deployed or has desired count zero.

The LLM service target layout is:

```text
services/product/llm_service/
├── Dockerfile.dockerignore
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── README.md
├── contracts/
│   └── v1/
│       └── openapi.json
├── scripts/
│   ├── start.sh
│   └── smoke_test.py
├── src/llm/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── bootstrap/
│   │   ├── __init__.py
│   │   ├── app_factory.py
│   │   └── lifespan.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py
│   │   ├── exception_handlers.py
│   │   ├── health.py
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   ├── access_log.py
│   │   │   ├── body_limit.py
│   │   │   └── security_headers.py
│   │   ├── security/
│   │   │   ├── __init__.py
│   │   │   ├── authentication.py
│   │   │   ├── authorization.py
│   │   │   └── rate_limit.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py
│   │       ├── schemas/
│   │       │   ├── __init__.py
│   │       │   ├── common.py
│   │       │   ├── chat.py
│   │       │   └── models.py
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── chat_completions.py
│   │           └── models.py
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── vllm.py
│   │   ├── sglang.py
│   │   └── transformers.py
│   └── observability/
│       ├── __init__.py
│       ├── context.py
│       └── logging/
│           ├── __init__.py
│           ├── config.py
│           ├── setup.py
│           ├── filters.py
│           ├── formatter.py
│           ├── daily_handler.py
│           └── active_session_handler.py
└── tests/
    ├── unit/
    │   ├── test_config.py
    │   ├── test_engine_selection.py
    │   └── test_streaming.py
    ├── integration/
    │   ├── test_health.py
    │   └── test_chat_completions.py
    └── contract/
        └── test_openai_compatible.py
```

The TTS service target layout is:

```text
services/product/tts_service/
├── Dockerfile.dockerignore
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── README.md
├── contracts/
│   └── v1/
│       └── openapi.json
├── scripts/
│   ├── start.sh
│   └── smoke_test.py
├── src/tts/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── bootstrap/
│   │   ├── __init__.py
│   │   ├── app_factory.py
│   │   └── lifespan.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py
│   │   ├── exception_handlers.py
│   │   ├── health.py
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   ├── access_log.py
│   │   │   ├── body_limit.py
│   │   │   └── security_headers.py
│   │   ├── security/
│   │   │   ├── __init__.py
│   │   │   ├── authentication.py
│   │   │   ├── authorization.py
│   │   │   └── rate_limit.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py
│   │       ├── schemas/
│   │       │   ├── __init__.py
│   │       │   ├── common.py
│   │       │   ├── speech.py
│   │       │   └── voices.py
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── speech.py
│   │           └── voices.py
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── vieneu.py
│   │   └── cosyvoice.py
│   └── observability/
│       ├── __init__.py
│       ├── context.py
│       └── logging/
│           ├── __init__.py
│           ├── config.py
│           ├── setup.py
│           ├── filters.py
│           ├── formatter.py
│           ├── daily_handler.py
│           └── active_session_handler.py
└── tests/
    ├── unit/
    │   ├── test_config.py
    │   ├── test_engine_selection.py
    │   └── test_audio_chunking.py
    ├── integration/
    │   ├── test_health.py
    │   ├── test_speech.py
    │   └── test_voices.py
    └── contract/
        └── test_tts_v1.py
```

The avatar service target layout is:

```text
services/product/avatar_service/
├── Dockerfile.dockerignore
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── README.md
├── contracts/
│   └── v1/
│       └── openapi.json
├── scripts/
│   ├── start.sh
│   └── smoke_test.py
├── src/avatar/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── sessions.py
│   ├── bootstrap/
│   │   ├── __init__.py
│   │   ├── app_factory.py
│   │   ├── container.py
│   │   └── lifespan.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py
│   │   ├── exception_handlers.py
│   │   ├── health.py
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   ├── access_log.py
│   │   │   ├── body_limit.py
│   │   │   └── security_headers.py
│   │   ├── security/
│   │   │   ├── __init__.py
│   │   │   ├── authentication.py
│   │   │   ├── authorization.py
│   │   │   └── rate_limit.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py
│   │       ├── schemas/
│   │       │   ├── __init__.py
│   │       │   ├── common.py
│   │       │   ├── avatars.py
│   │       │   └── sessions.py
│   │       └── routes/
│   │           ├── __init__.py
│   │           ├── avatars.py
│   │           └── sessions.py
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── avatarforcing.py
│   ├── publishing/
│   │   ├── __init__.py
│   │   └── livekit.py
│   └── observability/
│       ├── __init__.py
│       ├── context.py
│       └── logging/
│           ├── __init__.py
│           ├── config.py
│           ├── setup.py
│           ├── filters.py
│           ├── formatter.py
│           ├── daily_handler.py
│           └── active_session_handler.py
└── tests/
    ├── unit/
    │   ├── test_config.py
    │   ├── test_engine_selection.py
    │   └── test_session_state.py
    ├── integration/
    │   ├── test_health.py
    │   ├── test_avatars.py
    │   ├── test_session_lifecycle.py
    │   └── test_livekit_publish.py
    └── contract/
        └── test_avatar_v1.py
```

`main.py` remains a minimal entrypoint. LLM and TTS each own one active heavyweight engine, so `app_factory.py` plus `lifespan.py` can hold that resource without a redundant container abstraction. Avatar owns an engine, session registry, and LiveKit publisher registry with independent lifecycles, so its small typed `container.py` is justified. `inference.py`, `synthesis.py`, and `rendering.py` are not added: their only proposed job would duplicate the route-to-engine delegation already expressed by `dependencies.py` and `engines/base.py`. Avatar retains `sessions.py` because interruption, stop, cleanup, and LiveKit publication form a real lifecycle boundary.

Operational health endpoints remain outside `/api/v1` because orchestration probes are not part of the product contract. Versioned routes own LLM chat/model discovery, TTS speech/voice discovery, and avatar/session resources. Each service has explicit middleware for access logging, body limits, and response hardening, plus explicit security modules for service authentication, authorization scopes, and GPU-aware request/concurrency limiting. Redaction remains a logging concern in `observability/logging.py`.

The dependency directions are:

```text
LLM route -> engines/base.py -> active self-host engine
TTS route -> engines/base.py -> active self-host engine
Avatar route -> sessions.py -> engines/base.py + publishing/livekit.py
Backend -> application/clients/* -> self-host service or hosted provider
```

Configuration uses one vocabulary with one meaning per field:

```text
LLM_ENGINE=vllm|sglang|transformers
TTS_ENGINE=vieneu|cosyvoice
AVATAR_ENGINE=avatarforcing

LLM_ADAPTER=openai_compatible
TTS_ADAPTER=self_hosted|elevenlabs|openai_speech
AVATAR_ADAPTER=self_hosted|liveavatar|baidu_xiling

LLM_BASE_URL=
TTS_BASE_URL=
AVATAR_BASE_URL=
```

An `*_ENGINE` value always names executable self-host implementation code. An `*_ADAPTER` value names the backend outbound client/protocol. HTTP, SSE, WebSocket, and gRPC are transport semantics fixed by the selected adapter and versioned contract, not engine names or freely mixed environment values. Ambiguous selectors such as `openai_compat` as an engine, `remote_http`, and `remote_avatar` are not retained.

Backend provider credentials stay server-side. A cloud avatar adapter may return only browser-safe LiveKit URL and client-token data after authorization; provider secrets and provider session tokens remain in the backend. A separate provider-gateway runtime is introduced only when a measured requirement such as CPU-heavy media bridging, webhook termination, or transcoding gives it real ownership.

### 12. Keep platform runtimes upstream, real, and cost-aware

`services/platform/` owns only the repository assets required to configure, validate, package, and smoke-test upstream runtimes. It does not gain application packages, business logic, versioned product APIs, or copied upstream source. The target layout is:

```text
services/platform/
├── README.md
├── livekit/
│   ├── Dockerfile.dockerignore
│   ├── Dockerfile
│   ├── README.md
│   ├── entrypoint.sh
│   ├── livekit.yaml
│   └── scripts/
│       ├── validate_config.py
│       └── smoke_test.py
├── lmcache/
│   ├── Dockerfile.dockerignore
│   ├── Dockerfile
│   ├── README.md
│   ├── lmcache.yaml
│   └── scripts/
│       ├── validate_config.py
│       ├── smoke_test.py
│       └── benchmark.py
├── postgres/
│   ├── README.md
│   └── scripts/
│       └── smoke_test.py
└── redis/
    ├── README.md
    ├── redis.dev.conf
    └── scripts/
        └── smoke_test.py
```

One repository-root `compose.yaml` is the canonical local launcher. Its `data` profile starts the official Postgres and Redis containers, and its `media` profile starts the pinned local LiveKit wrapper. It contains no staging/production topology, cloud credential, or default GPU workload. Self-host LLM, TTS, and avatar are launched through their service-owned scripts because CUDA and model capacity are not a safe one-size-fits-all local profile.

LiveKit remains a thin local/sandbox wrapper around a version-and-digest-pinned upstream image. Its entrypoint may translate injected secret fields into the upstream configuration and then `exec` the server, but it fails startup when required credentials are missing. It does not install optional health tooling with ignored failures. Configuration validation and smoke checks target the real LiveKit process, signaling endpoint, and readiness behavior. Development, staging, and production use LiveKit Cloud rather than the current Fargate Spot service because the self-hosted WebRTC media plane needs stable direct UDP reachability and host-oriented networking. A future self-host decision, if justified by cost or data residency, uses dedicated on-demand VM capacity with public media reachability, TURN/TLS, and authenticated Redis rather than silently restoring the Fargate topology.

LMCache remains an optional upstream runtime, not a project-owned FastAPI service. Its image is pinned from a tested upstream standalone build, launches the real multiprocess server, and uses the upstream healthcheck and metrics surfaces. There is no `metrics_app.py`, best-effort package install, binary-missing fallback, or synthetic success response. `LMCACHE_ENABLED` defaults to false and infrastructure holds its desired count at zero until `benchmark.py` demonstrates a material latency, throughput, or GPU-cost benefit for the actual workload.

When enabled, the LMCache multiprocess server is colocated with vLLM on compatible GPU capacity and receives the required IPC/network topology. It is not scheduled as an independent ARM CPU skeleton. For the initial single-replica or low-load topology, local vLLM cache is sufficient and LMCache remains off. Scaling it independently becomes valid only after the selected upstream mode supports that topology and measurements justify the extra runtime.

Postgres and Redis use official upstream containers only for local development and ephemeral smoke tests. Stage and production use managed RDS and ElastiCache provisioned by `infra/modules/database/`; no custom production database image is built. Backend-owned schemas and raw SQL remain under `backend/db/sql/` and are applied by the backend migration/bootstrap path, never copied into `services/platform/postgres/`.

One managed Redis deployment initially serves backend sessions only. LiveKit Cloud owns its media-plane persistence and does not consume the project's ElastiCache instance. `redis.dev.conf` exists only to make the local official image safe and predictable; staging/production Redis parameters, security-group isolation, encryption, authentication, backups, and scaling remain Terraform concerns. Multi-node or Multi-AZ data services are introduced when a real availability objective or paid workload justifies their recurring cost.

The platform CI boundary is intentionally different from product Python CI:

```text
LiveKit change
  -> YAML validation -> ShellCheck -> Hadolint -> pinned-image check
  -> container build -> vulnerability scan -> real process smoke

LMCache change
  -> YAML validation -> pinned-image check -> container build
  -> vulnerability scan -> non-GPU real process smoke
  -> GPU integration and benchmark only in authorized staging

Postgres or Redis change
  -> config validation -> official-image scan -> ephemeral smoke
```

Platform pull-request CI never reports success by substituting a fake process for a missing upstream binary. GPU verification remains an explicit staging job so routine pull requests do not create GPU cost. Runtime logs are emitted to stdout/stderr and normalized by the existing collector into platform daily and active-session views; upstream containers do not receive project-specific observability source packages.

### 13. Make Workbench a modular developer tool, not a production frontend

`frontend/` is renamed to `workbench/`. The three current HTML entrypoints collapse into one developer console: the required Stage 2 behavior moves into one `index.html` and flat TypeScript modules, while `lite.html` and its legacy API usage disappear after canonical-contract verification. The chosen stack is Vite, vanilla TypeScript, Tailwind CSS, Vitest, and Playwright. React, Vue, Next.js, a Workbench application server, and speculative component abstractions are not added.

The target layout is:

```text
workbench/
├── README.md
├── package.json
├── package-lock.json
├── tsconfig.json
├── vite.config.ts
├── playwright.config.ts
├── eslint.config.js
├── index.html
├── src/
│   ├── main.ts
│   ├── styles.css
│   ├── api.ts
│   ├── api_types.ts
│   ├── websocket.ts
│   ├── state.ts
│   ├── sessions.ts
│   ├── resources.ts
│   ├── diagnostics.ts
│   ├── livekit.ts
│   ├── simulator.ts
│   ├── fixtures.ts
│   ├── dev_tokens.ts
│   └── fixtures/
│       ├── shop_profiles.json
│       ├── viewer_messages.json
│       └── products.json
├── scripts/
│   └── smoke_test.py
└── tests/
    ├── api.test.ts
    ├── state.test.ts
    ├── fixtures.test.ts
    ├── simulator.test.ts
    └── workbench.spec.ts
```

The modules follow existing responsibilities rather than a frontend framework convention. `main.ts` boots the page and binds events; `api.ts` owns canonical REST transport and safe response parsing; `api_types.ts` owns only DTOs used by Workbench; `websocket.ts` owns control and platform WebSocket connections; `state.ts` owns the single state/reducer; `sessions.ts` owns session actions; `resources.ts` owns avatar and voice discovery/selection; `diagnostics.ts` renders Director and queue state; `livekit.ts` joins the authorized room and consumes media directly; and `simulator.ts` emits deterministic viewer messages through the canonical platform WebSocket. `styles.css` imports Tailwind and holds only styling that utilities cannot express clearly.

Debug tooling moves by behavior rather than preserving the old Python package:

```text
core/debug/mock_data.py
  -> workbench/src/fixtures/shop_profiles.json
  -> workbench/src/fixtures/viewer_messages.json
  -> workbench/src/fixtures/products.json

core/debug/traffic_sim.py
  -> workbench/src/simulator.ts

core/debug/smoke.py
  -> workbench/scripts/smoke_test.py
```

The three JSON fixture files are explicit, versioned, non-secret test data. `shop_profiles.json` contains reusable shop/host/persona configurations; `viewer_messages.json` contains categorized viewer input including normal commerce, purchase intent, complaints, spam, off-topic, and safety cases; and `products.json` contains structured products that satisfy the canonical product schema. Workbench validates fixture shape before sending data. No real customer, shop, credential, or provider data is committed.

Workbench intentionally prefills two public local-only fixtures in `dev_tokens.ts`:

```text
LOCAL_VIEWER_TOKEN_FIXTURE
LOCAL_ADMIN_TOKEN_FIXTURE
```

These values optimize local iteration and are not treated as secrets. The local runtime matrix configures the backend with the same values when authentication is enabled. Backend startup validation rejects either known fixture whenever `APP_ENV` is not `dev` or `test`, so copying it into staging or production fails closed. Gitleaks receives one exact-value, path-scoped allowlist with a justification for `workbench/src/dev_tokens.ts`; no broad token rule, directory-wide exclusion, or entropy-detector disablement is allowed. Workbench keeps the values in page memory, does not persist them to local storage, and never receives provider secrets or LiveKit API secrets.

Workbench calls only the canonical session, avatar, voice, admin, control-WebSocket, and platform-WebSocket contracts. Viewer simulation happens in the browser and no `/api/v1/debug/*` router replaces the old debug code. `scripts/smoke_test.py` is an external black-box client and never imports backend internals. After equivalent behavior and contract tests pass, `core/debug/`, `frontend/lite.html`, the old static entrypoints, `/lite/*`, and `/api/v1/debug/*` are absent from the target tree and production route table.

Vite is a development server and static build tool here, not an additional deployed microservice. Workbench is not copied into the backend container and is not publicly deployed with production. An explicitly authorized staging check may publish its static build internally; otherwise CI retains only the build and Playwright artifacts. Browser media continues to arrive directly from LiveKit.

Verification is behavior-based. Vitest covers reducer transitions, API parsing, deterministic simulation, fixture validation, and non-persistence of tokens. Playwright covers one canonical flow from creating and configuring a session through viewer ingress, diagnostics, interruption, and stop. Brittle tests that only search substrings inside the monolithic `stage2.html` are replaced after equivalent behavior coverage exists.

### 14. Keep tests, contract artifacts, dependencies, and scripts with their real owners

Product-service correctness tests live beside the service but outside its importable `src/` package. Unit tests use no network, process, cloud service, or real database; integration tests may use the in-process app and ephemeral local Postgres or Redis; contract tests verify the service's versioned API and its consumers. Cross-service behavior alone lives at repository root. Real hosted-provider and GPU checks are explicit, authorized jobs rather than ordinary pull-request work.

The backend test layout is:

```text
services/product/backend_service/tests/
├── unit/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_session_context.py
│   ├── test_director_catalog.py
│   ├── test_director_decisions.py
│   ├── test_director_timers.py
│   ├── test_decision_preparation.py
│   ├── test_comment_clustering.py
│   ├── test_director_routing.py
│   ├── test_director_events.py
│   ├── test_playback_queue.py
│   ├── test_text_chunker.py
│   ├── test_run_plan.py
│   ├── test_api_security.py
│   ├── test_api_limits.py
│   └── test_observability.py
├── integration/
│   ├── conftest.py
│   ├── test_app_factory.py
│   ├── test_health_route.py
│   ├── test_session_routes.py
│   ├── test_avatar_routes.py
│   ├── test_voice_routes.py
│   ├── test_control_websocket.py
│   ├── test_platform_websocket.py
│   ├── test_playback_pipeline.py
│   ├── test_redis_session_store.py
│   ├── test_postgres_runtime_store.py
│   └── test_livekit_publishing.py
└── contract/
    ├── test_backend_openapi.py
    ├── test_llm_client_contract.py
    ├── test_tts_client_contract.py
    ├── test_avatar_client_contract.py
    └── test_livekit_client_contract.py
```

The self-host service suites remain smaller and responsibility-specific:

```text
services/product/llm_service/tests/
├── unit/{test_config.py,test_engine_selection.py,test_streaming.py}
├── integration/{test_health.py,test_chat_completions.py}
└── contract/test_openai_compatible.py

services/product/tts_service/tests/
├── unit/{test_config.py,test_engine_selection.py,test_audio_chunking.py}
├── integration/{test_health.py,test_speech.py,test_voices.py}
└── contract/test_tts_v1.py

services/product/avatar_service/tests/
├── unit/{test_config.py,test_engine_selection.py,test_session_state.py}
├── integration/{test_health.py,test_avatars.py,test_session_lifecycle.py,test_livekit_publish.py}
└── contract/test_avatar_v1.py
```

Repository-level tests are limited to behavior that cannot belong to one service:

```text
tests/
├── e2e/
│   ├── conftest.py
│   ├── test_stack_health.py
│   ├── test_session_lifecycle.py
│   ├── test_director_playback.py
│   └── test_livekit_delivery.py
└── sandbox/
    ├── conftest.py
    ├── test_liveavatar.py
    ├── test_hosted_llm.py
    └── test_hosted_tts.py
```

Sandbox tests are not selected by ordinary CI. When explicitly selected, missing credentials fail loudly rather than silently skipping. Real GPU inference runs only in authorized staging smoke jobs. Benchmarks measure performance and do not masquerade as correctness tests.

Each product service owns three distinct contract surfaces:

```text
src/<package>/api/v1/schemas/  source DTOs and WebSocket event models
contracts/v1/                 generated, committed API artifacts
tests/contract/               executable compatibility verification
```

There is no repository-root `contracts/` registry. Backend owns `contracts/v1/openapi.json` plus `contracts/v1/websocket/{control,platform}.schema.json`; LLM, TTS, and avatar each own `contracts/v1/openapi.json`. CI regenerates these files deterministically from routes and Pydantic schemas and fails on an uncommitted diff. A changed service contract fans out only to its real consumers, such as LLM plus backend LLM-client tests, or backend plus Workbench checks. Every product service keeps its unversioned operational health probes outside the versioned product contract, so no `/health/live` or `/health/ready` route appears in a v1 OpenAPI artifact.

Each product service has an independent `pyproject.toml` and `uv.lock`. A shared uv workspace lock is not used because LLM, TTS, and avatar may require incompatible Torch, CUDA, or provider dependency versions and must build and roll back independently. The repository root retains a non-package `pyproject.toml` plus `uv.lock` containing only cross-service E2E, sandbox, contract-generation, and repository-tool dependencies; product images and product CI never resolve runtime dependencies from it. Repository-level `.editorconfig`, `ruff.toml`, and `pyrightconfig.json` keep formatting and static-analysis policy consistent without coupling runtime resolution.

Container builds use the repository root as context so a service may consume approved shared scripts, but every built Dockerfile has an adjacent `Dockerfile.dockerignore`. The service-specific ignore file excludes unrelated services, Workbench, infrastructure, tests, documentation, runtime data, model weights, caches, and secrets while allowing only that service and explicitly required shared inputs. The root `.dockerignore` is a conservative fallback, not a second service policy.

Scripts follow the narrowest real owner:

```text
scripts/
├── ci/detect_changes.py
├── contracts/{generate.py,check.py}
└── model_assets/{fetch_weights.sh,upload.py}

benchmarks/
├── backend/{commerce_clustering.py,stage2_pipeline.py}
└── api/latency.py

infra/scripts/
├── staging_smoke.ps1
├── teardown_verify.ps1
└── swap_task_image.py
```

Service start, smoke, and engine-specific generation scripts remain in their owning service; Workbench smoke remains under `workbench/scripts/`. GitHub Actions remains the canonical deployment implementation, so a parallel root deployment-script layer is not added.

One root `.gitignore` covers Python, Node, test, build, runtime, model, secret, and Terraform outputs. Per-service `.gitignore` files are not added. Service `uv.lock` files, `package-lock.json`, generated committed contract artifacts, Workbench fixtures, Terraform dependency locks, `.gitleaks.toml`, and reviewed `.gitleaksignore` entries remain tracked.

Feature pushes run affected product-service unit tests only. Feature pull requests and merge commits run affected unit, integration, contract, branch coverage with `--cov-fail-under=80`, and cached container validation. Release verification adds affected cross-service E2E. Sandbox, real provider, GPU, and performance jobs remain explicit staging or manual work.

### 15. Preserve Terraform concerns while separating runtime services

Terraform remains organized by cloud concern rather than application name. The existing `network`, `security`, `compute`, `database`, `loadbalancer`, `storage`, `secrets`, and `monitoring` module boundaries remain canonical. `infra/modules/backend/`, `llm/`, `tts/`, or `avatar/` modules are not introduced because product ownership already lives under `services/product/` and duplicating it in Terraform would couple runtime layout to reusable cloud concerns.

The current `infra/modules/compute/main.tf` is split inside the same module without changing Terraform resource addresses:

```text
infra/modules/compute/
├── locals.tf
├── cluster.tf
├── iam.tf
├── discovery.tf
├── backend.tf
├── llm.tf
├── tts.tf
├── avatar.tf
├── variables.tf
├── outputs.tf
└── versions.tf
```

One ECS cluster per environment owns four independent product services. Backend uses Fargate; each selected self-host model service owns its task definition, desired count, capacity provider, health check, deployment, and rollback. LLM and TTS no longer share a task definition or fractional-GPU assumption. A hosted backend adapter forces the corresponding self-host desired count and minimum compute capacity to zero. LMCache has no standalone ASG or ECS service: it remains disabled until benchmark evidence exists and, when enabled, is colocated within the LLM GPU topology.

Only backend sits behind the public ALB. Backend reaches internal LLM, TTS, and avatar APIs through direct Cloud Map private DNS. An internal NLB and Service Connect proxy are not added for the initial zero-or-one-replica model topology; Service Connect becomes justified only after multiple replicas or measured retry/traffic-observability needs make the proxy useful.

The canonical environment roots and state keys are `global`, `dev`, `staging`, and `prod`. Terraform workspaces are not used as environment isolation. The runtime matrix is:

| Concern | Local | Dev AWS | Staging | Production |
|---|---|---|---|---|
| Backend | Local process | One Fargate Spot task | One on-demand Fargate task | Two on-demand Fargate tasks |
| Postgres/Redis | Optional official containers | Off by default; memory sessions | Single-AZ RDS and single-node ElastiCache | Single-AZ RDS and single-node ElastiCache initially |
| LLM/TTS/avatar | Mock, hosted, or optional self-host | Desired count zero by default | Conditional on selected adapter | Conditional on selected adapter |
| LiveKit | Local wrapper or Cloud | LiveKit Cloud | LiveKit Cloud | LiveKit Cloud |
| LMCache | Off | Off | Benchmark-only | Off unless approved evidence exists |
| Workbench/debug | Enabled | Explicit development use | Not deployed; debug off | Not deployed; debug off |

Single-AZ RDS and single-node Redis are an explicit MVP cost trade-off, not an availability claim. Multi-AZ is enabled when a documented SLO, paid traffic, or demonstrated downtime cost justifies it. Dev cloud resources are manually created and removable; keeping an unused ALB, database, Redis node, or GPU capacity running is not the default.

Backend receives only `LLM_ADAPTER`, `TTS_ADAPTER`, `AVATAR_ADAPTER`, matching base URLs, and service/provider credentials. Self-host tasks receive their own `LLM_ENGINE`, `TTS_ENGINE`, or `AVATAR_ENGINE`. Transport is fixed by the selected adapter implementation and versioned contract rather than environment values such as `remote_http` or `remote_avatar`.

Every deployed image is selected by immutable digest. Development and staging may build and push a digest after CI, while production promotes the exact staging-verified digest without rebuilding. ECS services use health checks and deployment circuit breakers; a failed rollout restores the previous task definition/digest. Runtime database migrations run once as an explicit pre-deploy backend task and remain additive/backward-compatible so an application rollback does not require an immediate destructive database rollback.

Terraform state uses separate S3 keys for each environment, bucket encryption, versioning, blocked public access, and native S3 lockfiles. DynamoDB locking may remain during a bounded migration while every runner adopts lockfiles, then is removed. GitHub OIDC roles are separated into plan, dev, staging, and production deployment roles and restrict their subject to the exact repository and protected GitHub environment. `iam:PassRole`, state access, ECS, and SSM permissions are scoped to the target environment.

Application, provider, LiveKit, viewer, and admin token plaintext does not pass through Terraform variables. Protected GitHub Environment secrets provision or update SSM SecureString values out of band, and ECS references their parameter ARNs. The `secrets` module retains naming and least-privilege integration policy without owning plaintext values. Under the deliberate no-Secrets-Manager MVP constraint, the RDS bootstrap password remains protected by the encrypted/restricted Terraform state; runtime application access uses a separate least-privilege database principal.

Infrastructure validation and mutation remain separate. `_infra-ci.yml` performs format, backend-free initialization, validation, native Terraform tests, and trusted plans when `infra/**` changes, but never applies. `infra-apply.yml` is a protected manual dispatch against an exact commit and applies its reviewed saved plan. `infra-teardown-nonprod.yml` can target only `dev` or `staging`, requires typed confirmation and approval, and cannot select production. Service deployment workflows update only images, task definitions, desired counts, and service rollout state; they never call Terraform apply.

## Risks / Trade-offs

- **[Integration can lead deployment]** `develop` may contain changes not yet deployed anywhere. -> Expose the deployed commit per environment and require an explicit commit SHA for dispatch.
- **[Invalid or mistyped dispatch input]** An operator may select the wrong service or SHA. -> Validate service values, branch ancestry, CI status, and show a deployment summary before the environment-changing job.
- **[Production tag bypass attempt]** A tag could target an unreviewed commit. -> Fail before deployment unless the commit belongs to `main` and has matching staging evidence.
- **[Required production approval is unavailable]** Repository plan or environment configuration may not enforce a reviewer gate. -> Fail production readiness validation and keep promotion disabled until the protected environment is configured; do not replace approval with an unreviewed tag push.
- **[Staging and production artifact drift]** Rebuilding can produce different image bytes. -> Promote the recorded staging digest without rebuilding.
- **[More workflow files]** Reusable workflows add indirection. -> Reserve underscore-prefixed files for `workflow_call` building blocks and keep all event triggers in the four named entry workflows.
- **[Secret value exposed by scanner output]** A detection log could repeat sensitive material. -> Force redacted Gitleaks output and retain only the rule, file, line, and fingerprint needed for remediation.
- **[False positive blocks integration]** A fixture or example may match a secret rule. -> Permit only narrowly scoped, reviewed allowlist entries; never globally disable a detector to pass CI.
- **[Cache contention across services]** A shared default BuildKit scope can overwrite unrelated cache entries. -> Use one stable GitHub Actions cache scope per service.
- **[Untrusted cache write]** An external pull request could attempt to influence a shared cache. -> Do not export shared cache entries from untrusted fork runs.
- **[Runtime config confused with cloud infrastructure]** Contributors may place Compose or runtime settings under Terraform modules. -> Keep runtime packaging under `services/platform/` and cloud provisioning under `infra/`.
- **[Async context leakage]** Concurrent requests could inherit another session's identifiers. -> Use `ContextVar` binding and guaranteed cleanup instead of process-global mutable state.
- **[Sensitive data in logs]** Prompts, tokens, or customer payloads could be logged accidentally. -> Allow only defined context fields, redact error details, and test that file and console handlers never emit secrets.
- **[Shared file contention after horizontal scaling]** Multiple replicas could corrupt a shared active log file. -> Keep runtime paths replica-local and aggregate stdout or files outside the process with service and instance labels.
- **[Runtime data changes prompt policy]** Shop or comment text could attempt to override system instructions. -> Delimit runtime values as untrusted data and compose immutable guardrails before task-specific instructions.
- **[Prompt drift is hard to reproduce]** Inline strings or arbitrary file overrides can change behavior without traceability. -> Version prompt files in Git and log only the bundle revision or hash.
- **[Composition container becomes a new god object]** Moving every operation into `container.py` would only rename the original problem. -> Limit it to typed resource construction and references; keep behavior in the owning services and use cases.
- **[Public and internal schemas drift together]** Reusing API DTOs as internal types can make a v1 contract change ripple through Director and playback code. -> Keep public DTOs under `api/v1/schemas/`, internal run-plan/utterance types under `application/schemas/`, and map them at the route boundary.
- **[Legacy or test routes remain reachable in production]** Leaving `/lite/*`, `/debug/*`, `/mock/*`, or sandbox endpoints mounted can preserve inconsistent contracts or expand attack surface. -> Migrate required workbench calls, assert the production OpenAPI/path table, and remove test-only mounts before release.
- **[Spot interruption removes the only backend task]** A one-task Spot service can disappear during the interruption window. -> Restrict Spot to manually deployed development, use on-demand Fargate in staging/production, and keep two production backend tasks.
- **[GPU services recreate hidden coupling]** Combining LLM/TTS in one task or assuming fractional ECS GPU allocation prevents independent rollout and capacity control. -> Give every self-host model an independent service/capacity contract and set unused services to zero.
- **[LiveKit self-host topology is unreliable]** Fargate Spot does not provide the stable host-oriented UDP topology expected by the media plane. -> Use LiveKit Cloud for the current stages; require a separate VM-based architecture decision before self-hosting.
- **[MVP data tier is not highly available]** Single-AZ RDS and single-node Redis can cause downtime. -> Document the accepted MVP ceiling, keep backups and deletion protection, and upgrade when SLO or paid-workload evidence justifies the recurring cost.
- **[Terraform exposes secret input in state]** Marking a variable sensitive only redacts output. -> Provision application/provider token values out of band into SSM and keep state encrypted and narrowly accessible for unavoidable infrastructure bootstrap data.
- **[Infrastructure apply races service deployment]** Coupling Terraform apply to an image rollout can mutate unrelated resources and complicate rollback. -> Use a protected infrastructure workflow and require service deployment to operate only against already-applied infrastructure.

## Migration Plan

1. Record the existing route/OpenAPI inventory, Director behavior fixtures, offline tests, and benchmark baselines; repair or add characterization coverage before moving responsibilities.
2. Introduce mandatory secret scanning, the stable CI gate, repository-aware affected paths, format/lint/type/coverage checks, and per-service Docker cache while both legacy and target paths still exist; enable no deployment trigger.
3. Create the product/platform ownership directories, independent product locks and containers, root tool-only lock, and compatibility imports so the old and new launch paths remain behaviorally equivalent before `core/` is removed.
4. Introduce `backend/main.py`, `bootstrap/`, unversioned operational health, shared middleware/security/errors, and version-owned business routes/schemas; retain a temporary old entrypoint and legacy route aliases until app-factory, route-inventory, and contract tests pass.
5. Refactor Director one behavior-preserving step at a time: session context, event/persistence adapter, decision preparation, playback worker, queue/chunking, then clustering/embeddings/scoring/routing; run the narrow Director suite and benchmark comparison after each extraction and do not mix algorithm changes into these commits.
6. Extract LLM, TTS, then avatar into self-host services with independent contracts and backend consumer tests; move hosted-provider clients into backend and remove redundant inference/synthesis/rendering layers only after each service passes unit, integration, contract, container, and backend-consumer gates.
7. Move upstream runtimes under `services/platform/`, add the local-only Compose profiles, pin real images/readiness, keep LMCache off, and switch non-local media to LiveKit Cloud.
8. Build Workbench, migrate fixtures/simulator/smoke behavior and canonical REST/WebSocket calls, verify Vitest/Playwright parity and direct LiveKit media, then remove debug, mock, sandbox-test, lite, and superseded frontend paths in a separate cleanup commit.
9. Move tests and contracts to their owners, generate deterministic contract artifacts, move benchmark programs/baselines and generated runtime data to their confirmed locations, and preserve any unclassified data or research material until its owner is known rather than deleting it.
10. Split the compute god file inside its existing module, add the staging root/state, and migrate combined LLM/TTS, standalone LMCache, LiveKit Fargate, and internal-NLB resources to the independent-service topology without changing unrelated module boundaries.
11. Add infrastructure validation, protected manual apply and non-production teardown, S3 lockfile migration, narrowed OIDC/IAM, immutable service deployment, staging evidence, and service-specific production promotion.
12. Remove `core/`, `frontend/`, `providers/`, old service directories, superseded workflows, mutable image references, and old infrastructure only after repository-wide reference search plus non-production dry runs prove every target path and rollback gate.

Rollback consists of disabling the new entry workflow triggers and restoring the last known workflow revision. Runtime rollback uses the previous recorded service digest.
