# Workflow graph audit — final state vs event-to-action matrix (OpenSpec 6.5)

Audit date: 2026-08-05. Base: c635f4f (refactor branch tip).

## Event-to-action matrix (design §4) — actual workflow mapping

| Event | Required result | Deployment | Workflow(s) that run | Mutation |
|---|---|---|---|---|
| Push to `feature/*` | Fast CI feedback | None | ci.yml (mode: feature-push) | none |
| PR `feature/*` → `develop` | Full integration CI | None | ci.yml (mode: feature-pr) | none |
| Merge into `develop` | CI on exact merge commit | None | ci.yml (mode: develop-merge) | none |
| PR `develop` → `main` | Full release CI | None | ci.yml (mode: release-pr) | none |
| Merge into `main` | CI on exact merge commit | None | ci.yml (mode: main-merge) | none |
| Dispatch deploy-dev.yml | Validate ref, deploy selected | Development | deploy-dev.yml | deploy |
| Dispatch deploy-staging.yml | Validate ref, deploy, record digests | Staging | deploy-staging.yml | deploy |
| Push eligible service tag (`*-v*`) | Validate release evidence, deploy one service | Production | release-service.yml | deploy |
| Dispatch build-images.yml | Build + push SHA images | None | build-images.yml | artifact_push only |
| Dispatch seed-weights.yml | Seed HF weights to S3 | None | seed-weights.yml | none |
| Dispatch infra-apply.yml | Plan + apply ONE reviewed plan | Infra dev/staging | infra-apply.yml | infra_mutation |
| Dispatch infra-teardown-nonprod.yml | Destroy dev/staging after typed confirm | Infra dev/staging | infra-teardown-nonprod.yml | infra_mutation |

Verified: `scripts/ci/inventory_workflows.py --repo-root .` classifies every
workflow; `scripts/ci/static_validate_workflows.py` passes all rules (R1-R12).

## Workflow graph (12 files, 4 reusable + 8 event-entry)

```
entry: ci.yml (push feature/**|develop|main, PR into develop|main)
  └─ _python-service-ci.yml   (workflow_call — per-service ruff/pyright/pytest/coverage)
  └─ _container-build.yml     (workflow_call — buildx, gha cache, push flag; PR push:false)
entry: deploy-dev.yml (dispatch: commit_sha + services)
  └─ _container-build.yml (push:true, dev-<sha>)
  └─ _deploy-service.yml  (workflow_call — image swap, service-scoped rollback, smoke)
entry: deploy-staging.yml (dispatch: commit_sha + services)
  └─ _container-build.yml (push:true, staging-<sha>)
  └─ _deploy-service.yml
  └─ record-evidence (commits deploy-evidence/staging/<sha>.jsonl to main)
entry: release-service.yml (push tag *-v*)
  └─ _deploy-service.yml (EXACT staging digest @sha256, no rebuild)
entry: build-images.yml (dispatch — offline image prep, dev-<sha> tags)
entry: seed-weights.yml (dispatch — HF → S3)
entry: infra-apply.yml (dispatch — plan/apply, env infra-<env>)
entry: infra-teardown-nonprod.yml (dispatch — destroy, hard dev|staging allowlist)
entry: deploy-prod.yml (SUPERSEDED — triggers disabled by 6.4: `on: {}`, zero live triggers)
```

Ruleset enforcement (3.4): `CI / gate` is the single stable required check on
develop + main. Rulesets deny direct pushes; PRs require 1 approval, resolved
conversations, conflict-free current head. Verified in `docs/branch-rulesets.md`
+ `.github/rulesets/{develop-protection,main-protection}.json`.

## Audit findings

1. **No implicit deployment on CI.** ci.yml has no workflow_dispatch, no tag
   trigger, no schedule; mutation classification = all false; no deploy-capable
   action or keyword in any job (R9 verified by test + static validator).
2. **Affected-area fan-out** (2.4): service contract/source-DTO changes fan to
   exact consumers (backend_service ↔ workbench for backend; owner + backend
   for llm/tts/avatar); root shared config/locks/build map to explicit shared
   areas; unknown paths are conservative shared-source (never silent).
3. **Stable gate:** `CI / gate` aggregates secret-scan, affected-area, mode,
   service-ci, container-build, workbench-check, platform-check,
   terraform-plan; skipped unaffected jobs report neutral success (3.3).
4. **Reusable isolation:** `_python-service-ci`, `_container-build`,
   `_deploy-service` are workflow_call only, underscore-prefixed, no
   environment reference (R2/R3 enforced).
5. **Deployment is explicit-only:** every deploy-capable mutation is behind
   workflow_dispatch or a service tag; no push/PR path deploys.
6. **Production path is digest-exact:** release-service promotes the staging
   evidence digest only; deploy-prod (mutable tag build+deploy) is superseded
   and its triggers disabled (6.4) — file retained with `on: {}` for a
   one-commit revert.
7. **build-images.yml kept active** (6.4 check): it has 20 historical
   dispatch runs (offline image prep for the three-stage boot), is exercised
   by `infra/tests/test_platform_roots.py`, pushes only `dev-<sha>` tags
   (artifact_push, never deploy), and is the documented offline build path.
   No disable applied.

## Residual admin-side items (not code — see cluster report)

- Rulesets `develop-protection` / `main-protection` exist as JSON but are not
  yet applied on GitHub (repo currently shows zero rulesets via API).
- GitHub Environments `staging`, `infra-dev`, `infra-staging` do not exist yet.
- `production` environment required-reviewers not configured.
- `release-service.yml` / `deploy-staging.yml` / `_deploy-service.yml` are not
  yet on the remote main branch (refactor branch not pushed/merged).
