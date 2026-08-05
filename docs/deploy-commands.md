# Deployment commands (dev + staging) — no web console needed

Development and staging deployments are explicit, terminal-driven operations
(OpenSpec 4.3). There is no auto-deploy on push: the deployment workflows only
start when dispatched, and the workflow itself validates the commit and CI
gate before touching an environment.

## Prerequisites

- `gh` CLI installed and authenticated: `gh auth status`.
- The commit to deploy is a **full 40-hex SHA** and is reachable from the
  required branch:
  - `deploy-dev.yml` dispatches from `develop`; the commit must be an
    ancestor of `develop`.
  - `deploy-staging.yml` dispatches from `main`; the commit must be contained
    in `main`.
- The commit has a successful `ci.yml` run (the workflow fails closed
  otherwise).
- Environment/secret prerequisites:
  - dev: GitHub Environment `development` exists; secrets `AWS_ROLE_ARN_DEV`,
    `DOCKERHUB_USER`, `DOCKERHUB_TOKEN`; SSM parameters `/dev/backend/api_token`
    and `/dev/admin/api_token` provisioned out-of-band.
  - staging: GitHub Environment `staging` exists (admin-apply: see cluster
    report); secret `AWS_ROLE_ARN_STAGING` (role from
    `infra/environments/global` with `github_environment = "staging"`); SSM
    parameters `/staging/backend/api_token` and `/staging/admin/api_token`
    provisioned out-of-band.

## Direct gh commands

```bash
# Deploy backend + TTS to DEV (from the develop branch context)
gh workflow run deploy-dev.yml --ref develop -f commit_sha=<full-40-hex-sha> -f services=backend_service,tts_service

# Deploy backend + TTS to STAGING (from the main branch context)
gh workflow run deploy-staging.yml --ref main -f commit_sha=<full-40-hex-sha> -f services=backend_service,tts_service
```

Supported service identifiers: `backend_service`, `llm_service`,
`tts_service`, `avatar_service` (comma-separated, no spaces).

The GitHub web UI **Run workflow** button and the REST API
(`POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches`) invoke the
same workflow with identical behavior and validation — the workflow, not the
console, is the source of truth.

### Input equivalence (6.2)

All three entry points (gh CLI, web UI, REST API) resolve to the same
`workflow_dispatch` payload; the workflow performs identical validation
regardless of caller:

- `commit_sha` — full 40-hex SHA resolving to a commit, reachable from the
  dispatch ref's branch (develop for dev, main for staging), with a completed
  successful `ci.yml` run for that SHA (fail closed).
- `services` — comma-separated canonical identifiers from
  `backend_service,llm_service,tts_service,avatar_service`; unknown, mixed
  case, empty, or shell-metacharacter entries are rejected by
  `scripts/ci/validate_workflow_inputs.py`.

Verified locally: wrapper argument validation (`deploy.sh`/`deploy.ps1`),
profile binding (`deploy-dev`→dev, `deploy-staging`→staging), and the shared
validator's rejection paths (bad SHA, unknown service, injection-shaped
input). The wrappers are thin: they pass the exact `-f` key/value pairs the
workflow declares, so a dispatch from any interface hits the same validation
and the same jobs.

## Wrapper scripts

Thin wrappers around the commands above (same validation, no extra logic):

```powershell
# PowerShell (Windows operator)
scripts/deploy.ps1 -Env dev -Sha <sha> -Services backend_service,tts_service
scripts/deploy.ps1 -Env staging -Sha <sha> -Services backend_service,tts_service
```

```bash
# POSIX shell
scripts/deploy.sh dev <sha> backend_service,tts_service
scripts/deploy.sh staging <sha> backend_service,tts_service
```

Both fail with a non-zero exit and an instruction if `gh` is not
authenticated, the SHA is not a full 40-hex string, or the environment is not
`dev`/`staging`. They print the workflow run URL after dispatch; add
`-Watch` (PowerShell) / pass `watch` as the last argument (shell) to follow
the run to completion.

## Watching and verifying

```bash
gh run list --workflow deploy-dev.yml -L 5
gh run watch --exit-status   # after dispatch; fails non-zero if the run fails
```

The deployment job summary contains the per-service previous/new digest,
smoke URL, and result. Deployment evidence is also recorded (4.4).

## Deployment evidence (4.4)

Every deployment records one JSON line per deployed service with the fields:

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp of the evidence write |
| `env` | `dev` / `staging` |
| `commit_sha` | The exact deployed commit |
| `service` | Canonical service id (`backend_service`, ...) |
| `initiator` | `github.actor` who dispatched the run |
| `prev_digest` | Image ref previously deployed on the service |
| `new_digest` | Immutable digest (or exact image ref) deployed |
| `result` | `success` / `failure` |

Locations:

- Runtime audit trail (gitignored): `.runtime/deploy/evidence/<env>/<sha>.jsonl`
  — written by the deploy workflows on every run.
- Production-eligible record (tracked): `deploy-evidence/staging/<sha>.jsonl`
  — committed to `main` by `deploy-staging.yml` only after a successful
  smoke. This file is the staging evidence that `release-service.yml` (task
  5.2) validates before allowing a production release of that service and
  commit, and it carries the exact image digest that production promotes
  without rebuilding (5.4).

Example line:

```json
{"ts":"2026-08-05T12:00:00Z","env":"staging","commit_sha":"abcdef0123456789abcdef0123456789abcdef01","service":"backend_service","initiator":"justHman","prev_digest":"imjusthman/ai-live-backend@sha256:aaaa...","new_digest":"imjusthman/ai-live-backend@sha256:bbbb...","result":"success"}
```
