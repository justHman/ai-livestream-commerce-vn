## ADDED Requirements

### Requirement: Tracked workflow inventory
The repository SHALL maintain a concise tracked workflow inventory covering every file under `.github/workflows/`, its trigger/event/filter, reusable versus event-entry role, jobs and actions, artifact/deploy/infra mutation, permissions/secrets/environment references, and its canonical target or removal condition. The inventory SHALL identify implicit deployments, path-skipped required checks, stale service paths, and overlapping release paths without changing any trigger.

#### Scenario: Inventory all workflows
- **WHEN** the inventory script runs over `.github/workflows/`
- **THEN** every `*.yml` file is inventoried with structured triggers, jobs, actions, permissions, environment, and concurrency and the inventory reports parse failures rather than silently skipping a file

#### Scenario: Confirm a trigger
- **WHEN** an operator inspects the inventory for `ci.yml`
- **THEN** the inventory shows only `push` and `pull_request` triggers and no `workflow_dispatch`

#### Scenario: Detect overlapping release paths
- **WHEN** multiple workflows reference the same image push or ECS mutation
- **THEN** the inventory surfaces the shared actions and environment references so overlapping release behavior is visible before refactoring

### Requirement: Validated workflow inputs
Dispatch and release workflows SHALL validate an immutable commit SHA, a target environment, and a supported service list through one shared reusable validation script before changing any environment. The SHA MUST be a full 40-hexadecimal commit object that resolves in the repository; the environment MUST be an exact lowercase allowlist value per workflow; the service list MUST be a comma-separated, normalized, deduplicated, non-empty subset of the canonical `backend_service,llm_service,tts_service,avatar_service` identifiers, with no shell-injection or whitespace ambiguity. The CLI SHALL fail when any input required for the selected mode is missing rather than returning success. Validation SHALL emit safe JSON/matrix outputs through `GITHUB_OUTPUT` and MUST NOT use `eval`.

#### Scenario: Deploy a valid SHA
- **WHEN** an operator dispatches a deployment with the full 40-hex commit SHA that resolves in the repository
- **THEN** validation returns the normalized SHA and emits `validated_sha` plus a safe services matrix through `GITHUB_OUTPUT`

#### Scenario: Reject a short SHA
- **WHEN** a dispatch specifies a shortened SHA
- **THEN** validation rejects it with a message requiring a full 40-character hexadecimal SHA before any environment change

#### Scenario: Reject an unsupported environment
- **WHEN** a dispatch specifies an environment outside the workflow's exact allowlist
- **THEN** validation rejects it with the allowed set and no environment is changed

#### Scenario: Reject an invalid service
- **WHEN** a dispatch specifies an unknown, mixed-case, short-form, or shell-injected service identifier
- **THEN** validation rejects it before deployment and reports the unknown value without echoing untrusted content into a shell command

#### Scenario: Execute without required inputs
- **WHEN** the validation CLI is invoked without the `--sha`, `--env`, or `--services` inputs it declares required
- **THEN** the CLI exits non-zero and names each missing required input before any environment change

### Requirement: Static workflow validation
The repository SHALL validate workflow YAML statically, rejecting unsupported triggers, invalid reusable-workflow references, and malformed service tag patterns. YAML SHALL be parsed with a pinned/declared tooling dependency or a narrow stdlib parser proven correct. Event entry names SHALL follow the descriptive convention and underscore-prefixed reusable workflows SHALL expose `workflow_call` only. Local reusable references MUST exist and use the allowed `./.github/workflows/<name>.yml` form. Reusable workflows MUST NOT declare push, pull_request, schedule, repository_dispatch, or workflow_dispatch triggers. Service tags MUST match the exact `<service>-vSEMVER` pattern. CI MUST NOT contain an implicit deployment step. Permission, environment, and secret reference shape SHALL be validated without reading values.

#### Scenario: Validate the repository workflows
- **WHEN** the static validator runs against `.github/workflows/`
- **THEN** every workflow passes or fails with a rule-specific message and the process exits non-zero when any workflow fails

#### Scenario: Reject an unsupported trigger
- **WHEN** a workflow declares a trigger not in the supported event entry set
- **THEN** validation fails and names the unsupported trigger

#### Scenario: Reject a broken reusable reference
- **WHEN** a job references a reusable workflow that does not exist or uses an invalid ref form
- **THEN** validation fails and identifies the missing file or malformed ref

#### Scenario: Reject a reusable workflow with an entry trigger
- **WHEN** a reusable workflow declares a push, pull_request, schedule, or workflow_dispatch trigger
- **THEN** validation fails because reusable workflows must use `workflow_call` only

#### Scenario: Reject a malformed service tag
- **WHEN** a workflow references a service tag that does not match `<service>-vSEMVER`
- **THEN** validation fails and names the allowed service tag format

### Requirement: Repository-aware affected areas
CI SHALL compute a deterministic affected-area map from changed paths and fan out only required areas. Direct owner paths select their owner. A service contract artifact or a canonical source DTO under `services/product/<service>/src/<pkg>/api/v1/schemas/` selects its owner plus the exact consumers: backend contract fans to backend and Workbench, and LLM, TTS, and avatar contracts fan to their owner plus backend. Backend shared schema roots fan to backend and Workbench. Root shared source-policy, dependency-lock, build, and CI files map to their explicit shared areas (`shared-config`, `shared-locks`, `shared-build`, or `ci`) and never fan every service. Renames and deletes SHALL be handled safely. Docs-only changes SHALL be neutral unless runtime docs are consumed by the build. Unknown paths MUST NOT be silently dropped and MUST NOT fan every service; they classify to a conservative single shared-source area.

#### Scenario: Change a product service
- **WHEN** a change touches `services/product/backend_service/` source
- **THEN** the detector emits `backend_service` and no unrelated area

#### Scenario: Change a service contract
- **WHEN** a change touches `services/product/tts_service/contracts/`
- **THEN** the detector emits `tts_service` plus the consuming backend client area and never recursively fans to Workbench

#### Scenario: Change a canonical source DTO
- **WHEN** a change touches `services/product/backend_service/src/backend/api/v1/schemas/`
- **THEN** the detector emits `backend_service` plus `workbench`; a LLM, TTS, or avatar DTO change emits its owner plus `backend_service`

#### Scenario: Change the backend legacy contract root
- **WHEN** a change touches the backend `contracts/` or shared `core/api/v1/schemas/` roots
- **THEN** the detector emits `backend_service` plus `workbench`

#### Scenario: Change shared configuration
- **WHEN** a change touches `pyproject.toml`, `uv.lock`, `ruff.toml`, `pyrightconfig.json`, or `.github/`
- **THEN** the detector emits only the corresponding shared-config, shared-locks, shared-config, or CI area and no other area

#### Scenario: Change a docs-only file
- **WHEN** a change touches only `docs/` or `README.md`
- **THEN** the detector emits no required area (neutral)

#### Scenario: Encounter an unknown path
- **WHEN** a changed path matches no known owner
- **THEN** the detector emits the conservative single `shared-source` area and never drops the path silently
