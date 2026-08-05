# Cluster 4.x + 5.x report — deployment & production release (dev/staging deploy + infra apply/teardown + release-service)

Status: DONE_WITH_CONCERNS
Base: 221b8e2 (refactor branch tip). Worktree: agent-a65e675a9579034a1. 7 commits.

## Per-task results

| Task | Commit | What changed | Verification |
|---|---|---|---|
| 4.1 deploy-dev.yml | 9fe476e | Rewrote to dispatch-only (`commit_sha`+`services` inputs); validate job (SHA/profile validation via scripts/ci/validate_workflow_inputs.py, develop ancestry, CI gate via gh api fail-closed); preflight (terraform outputs, SSM secret existence check — provisioning dropped, role lacks PutParameter); build via _container-build matrix (selected services only, tag dev-<sha>); deploy via new _deploy-service reusable (migration task, smoke, rollback) | YAML parses; static validator PASS; tests/ci 160 pass (2 pre-existing base failures) |
| 4.2 deploy-staging.yml | e701688 | New dispatch-only workflow: main ancestry + CI gate validation; build tag staging-<sha>; deploy via _deploy-service; Stage-1 API smoke (staging_smoke.ps1 via pwsh, tokens from SSM); record-evidence job commits deploy-evidence/staging/<sha>.jsonl to main (bot push, fail-closed on ruleset reject) | YAML parses; static validator PASS; tests pass |
| 4.3 gh CLI docs/wrappers | 9ef7162 | docs/deploy-commands.md (exact design §3 commands), scripts/deploy.sh + scripts/deploy.ps1 (env allowlist, SHA check, gh dispatch, optional watch) | bash -n OK; commands match workflow inputs; pwsh unavailable locally (noted) |
| 4.4 evidence recording | 9ef7162 | Schema documented (ts/env/commit_sha/service/initiator/prev_digest/new_digest/result); runtime `.runtime/deploy/evidence/<env>/<sha>.jsonl` (written by workflows) + tracked `deploy-evidence/staging/<sha>.jsonl` (production-eligible, committed) | Format matches across dev/staging/release workflows |
| 4.5 infra-apply.yml | cb66227 | Protected manual exact-commit workflow: validate (hard dev|staging allowlist, SHA resolve), plan (env `infra-<env>`, terraform plan → ONE saved plan artifact + printed review), apply (download artifact, apply EXACTLY that plan); TF_VAR_* from env variables/secrets or optional var_file | YAML parses; static validator PASS; no prod path |
| 4.6 infra-teardown-nonprod.yml | cb66227 | Protected: hard dev|staging allowlist (prod impossible — rejected before any job, no prod branch), typed confirmation `teardown-<env>`, plan-destroy review artifact, destroy applies the reviewed plan; global env untouched | YAML parses; static validator PASS; "prod" appears only in impossibility docs |
| 5.1 release-service.yml tag parsing | 720e23e | New workflow on push tags `*-v*`; resolve job parses via validate_service_tag (backend/llm/tts/avatar-vSEMVER), resolves tag commit | Simulated tag parse + evidence extraction locally OK |
| 5.2 main ancestry + staging evidence | 720e23e | resolve job requires tag commit contained in origin/main; validate-evidence job reads deploy-evidence/staging/<sha>.jsonl (origin/main tree, fallback tag tree), requires result=success line for service+commit, extracts digest; fails closed | Evidence parse simulated with sample JSONL |
| 5.3 production approval | 720e23e + 923c47b | deploy job `environment: production` + readiness step: gh api checks required_reviewers protection rule exists, FAILS CLOSED if absent; admin-apply documented (docs/production-release.md); self-approval prevented by required reviewer distinct from releasing actor (GitHub has no per-env self-approval toggle — documented) | Code verified; env-approval behavior needs GitHub (not locally verifiable) |
| 5.4 exact-digest promotion | 720e23e | Deploy uses ONLY the staging evidence digest (`@sha256:` asserted), no _container-build call, no mutable tags; digest manifest verified in Docker Hub before ECS update | grep: no build call, no `:dev-`/`:staging-` refs; @sha256 assertion present |
| 5.5 prod smoke + service-scoped rollback | 720e23e + 923c47b | Backend smoke via curl /api/v1/health/live (12x retry, fail-closed); _deploy-service rolls back ONLY the affected service to its previous task definition (previous digest) on failure | Grep verified rollback + smoke in reusable |

## Workflows inventory (final, 12 files)
ci.yml (push/PR, gate) · _python-service-ci.yml · _container-build.yml · _deploy-service.yml (NEW reusable, workflow_call only, no environment/triggers) · deploy-dev.yml (dispatch) · deploy-staging.yml (NEW, dispatch) · deploy-prod.yml (superseded — trigger disable is 6.4, untouched) · release-service.yml (NEW, tag push) · infra-apply.yml (NEW, dispatch, protected) · infra-teardown-nonprod.yml (NEW, dispatch, protected) · build-images.yml · seed-weights.yml

## Evidence format (4.4)
One JSON line per service: `{ts, env, commit_sha, service, initiator, prev_digest, new_digest, result}`.
- Runtime (gitignored): `.runtime/deploy/evidence/<env>/<sha>.jsonl` — every deploy/release run.
- Production-eligible (tracked): `deploy-evidence/staging/<sha>.jsonl` — committed by deploy-staging after smoke; read by release-service (5.2) for the exact digest.

## Admin-apply instructions (GitHub-side, cannot verify locally)
1. Create GitHub Environment `staging` (secrets: AWS_ROLE_ARN_STAGING, DOCKERHUB_USER, DOCKERHUB_TOKEN; optional required reviewers).
2. Apply `infra/environments/global` with `github_environment = "staging"` to create `ai-livestream-github-deploy-staging` + plan role; set AWS_ROLE_ARN_STAGING to the deploy role ARN.
3. Create protected environments `infra-dev` + `infra-staging` (required reviewers — approval gate for infra-apply/teardown); secrets AWS_ROLE_ARN_INFRA_DEV/STAGING = AWS role trusted for environment `infra-<env>` with the environment's Terraform apply/destroy IAM (this role does not exist yet — create it; the existing deploy/plan roles lack full terraform apply perms).
4. Configure `production` environment: required reviewers (approver distinct from releasing operator), secrets AWS_ROLE_ARN_PROD/DOCKERHUB_* already exist; verify the workflow's readiness check passes (required_reviewers rule present).
5. Provision SSM SecureString params out-of-band: `/dev/backend/api_token`, `/dev/admin/api_token`, `/staging/backend/api_token`, `/staging/admin/api_token` (deploy roles lack ssm:PutParameter — verified in global main.tf policy).
6. The main ruleset must allow the `ai-live-deploy-bot` evidence commit, OR the operator lands `deploy-evidence/staging/<sha>.jsonl` via PR before tagging a release (workflow fails closed with instructions otherwise).

## Commit SHAs
228720c (ledger) → 9fe476e (4.1) → e701688 (4.2) → 9ef7162 (4.3+4.4) → cb66227 (4.5+4.6) → 720e23e (5.1+5.2) → 923c47b (5.3-5.5 docs) → 490ec87 (ledger final)

## Mini-ledger pointer
`.superpowers/sdd/tasks/cluster-4x5x-ledger.md`

## Concerns
- Implementer subagent model failed (worktree .claude/settings.json Write-deny rules + empty execution) → supervisor implemented all batches directly (allowed by brief fix-loop: resume ≤3 then do it yourself).
- 2 PRE-EXISTING test failures in tests/ci/test_inventory_workflows.py (test_every_workflow_has_canonical_target, test_step_uses_captured) exist at base 221b8e2 — NOT caused by this cluster; out of scope (1.59/6.x test ownership).
- GitHub-side verifications pending (cannot run locally): environment approval behavior (5.3), bot evidence commit vs main ruleset (4.2), actionlint not available (YAML parse + static validator used).
- deploy-prod.yml still has its v* tag trigger — disabling is task 6.4 (next cluster).
- infra-apply/teardown AWS roles (AWS_ROLE_ARN_INFRA_*) do not exist yet — admin-apply per above.
