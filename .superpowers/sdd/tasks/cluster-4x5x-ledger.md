# Cluster 4.x + 5.x ledger — deployment and production release (dev/staging deploy, infra apply/teardown, release-service)

Credentials are referenced only as GitHub secrets expressions. No literal secret values appear in workflow files, tests, or docs.

Base: 221b8e2 (refactor branch tip; 8a55e91 + 1.79 review fix). Worktree: agent-a65e675a9579034a1.
Brief: .superpowers/sdd/tasks/`task-4x5x`-supervisor-brief.md (tasks.md §4.1-4.6 + §5.1-5.5).

## Workflows inventory (post-3.x, base 221b8e2)
| File | Trigger | Role | Deployment |
|---|---|---|---|
| ci.yml | push feature/**/develop/main + PR into develop/main | entry CI, secret-scan + gate | none |
| _python-service-ci.yml | workflow_call | reusable per-service checks | none |
| _container-build.yml | workflow_call | reusable buildx build (gha cache, push flag) | none (push only when caller sets push:true) |
| deploy-dev.yml | push develop (paths) + NO dispatch | auto DEV deploy (preflight/build/deploy+rollback via ALB health) | DEV auto |
| deploy-prod.yml | push tag v* + dispatch confirm | build tag images + manual PROD deploy | PROD manual |
| build-images.yml | workflow_dispatch | build+push SHA images only | none |
| seed-weights.yml | workflow_dispatch | HF weights to S3 | none |

Note: deploy-dev currently auto-deploys on push develop; 4.1 converts to explicit workflow_dispatch (design §3).
deploy-prod build+deploy path is superseded by release-service.yml (5.x); 6.4 disables superseded triggers — out of cluster scope (coordinator).

## Design/constraint notes (for batches)
- env `development` / `production` exist in repo; `staging` env must be created via admin-apply instructions. Secrets: AWS_ROLE_ARN_DEV, AWS_ROLE_ARN_PROD exist; AWS_ROLE_ARN_STAGING must be created.
- _deploy-service.yml reusable (workflow_call, NO environment, NO deployment trigger) — container image swap + ECS update + rollback + ALB target health; env-specific values (role ARN, env name, cluster/service names, image repo, platform) passed as inputs.
- Smoke: dev = ALB target health (existing); staging smoke/E2E = infra/scripts/staging_smoke.ps1 requires ALB base URL + token (operator-provided input); prod smoke = ALB target health + minimal API health (no existing runtime E2E script for prod).
- Evidence: per-env committed JSONL under `.runtime/deploy/evidence/` (runtime gitignored) + GHA job summary (4.4). Release evidence = staging evidence file + digest; format: {ts, env, commit_sha, service, initiator, prev_digest, new_digest, result}.
- Digest: build tags `staging-<sha>`; digest recorded via `docker buildx imagetools inspect --format {{.Manifest.Digest}}`.
- prod does NOT rebuild; release-service.yml reads staging evidence + exact digest.
- infra-apply/infra-teardown: protected envs `infra-dev`/`infra-staging` (per-apply env target), approvals, typed confirmation (TYPE), teardown allowlist dev|staging hard.
- Test updates REQUIRED: tests/ci/test_inventory_workflows.py expects deploy-dev push trigger + deploy-prod v* tags + path_filters — update for dispatch-only triggers; test_static_validate_workflows R4 (ci no dispatch) unaffected.

## Execution

### Batch 0 — ledger + audit baseline
- 228720c chore(sdd): 4x5x cluster ledger (audit baseline, batch 0)

### Batch 1 — 4.1 deploy-dev.yml dispatch rewrite + _deploy-service.yml reusable
- DONE 9fe476e ci(dev): dispatch-only deploy-dev + _deploy-service reusable (4.1)
- Implementer subagent FAILED (settings Write-deny + empty exec); supervisor implemented directly.
- Verified: YAML parses, static validator all PASS, tests 160 pass (2 pre-existing base failures).

### Batch 2 — 4.2 deploy-staging.yml
- DONE e701688 ci(staging): dispatch-only deploy-staging with evidence recording (4.2)
- Verified: YAML parses, static validator all PASS, tests pass (2 pre-existing failures).
- Evidence committed to deploy-evidence/staging/<sha>.jsonl on main after smoke (release gate reads it).

### Batch 3 — 4.3 + 4.4 gh CLI docs + evidence recording
- DONE 9ef7162 docs(deploy): gh CLI deploy commands + wrappers + evidence format (4.3, 4.4)
- docs/deploy-commands.md + scripts/deploy.sh + scripts/deploy.ps1; bash -n OK; pwsh not available locally (noted).

### Batch 4 — 4.5 + 4.6 infra-apply.yml + infra-teardown-nonprod.yml
- DONE cb66227 ci(infra): protected infra-apply + infra-teardown-nonprod workflows (4.5, 4.6)
- Verified: YAML parses, static validator all PASS, hard allowlist dev|staging, typed confirmation, no prod path.

### Batch 5 — 5.1 + 5.2 release-service.yml
- DONE 720e23e ci(release): service-tag release workflow with staging evidence gate (5.1, 5.2)
- Tag parse via validate_service_tag; main ancestry + staging evidence gate; evidence parse simulated locally OK.
- Deploy job included (promotion) to avoid broken intermediate state.

### Batch 6 — 5.3 + 5.4 + 5.5 production approval + promotion + smoke + rollback
- DONE 923c47b docs(deploy): production release flow + approval admin-apply (5.3-5.5)
- 5.3 readiness check (required_reviewers fail-closed), 5.4 digest-only promotion, 5.5 smoke + service-scoped rollback all in release-service.yml + _deploy-service.yml (verified by grep).

## FINAL
- 7 commits: 228720c (ledger), 9fe476e, e701688, 9ef7162, cb66227, 720e23e, 923c47b
- Workflows inventory (final, 12 files): ci, _python-service-ci, _container-build, _deploy-service, deploy-dev (dispatch), deploy-staging (dispatch), deploy-prod (superseded, 6.4), release-service, build-images, seed-weights, infra-apply, infra-teardown-nonprod
- Pre-existing test failures at base 221b8e2 (NOT caused by this cluster): test_every_workflow_has_canonical_target, test_step_uses_captured

## Key decisions (supervisor)
- Evidence: .runtime/deploy/evidence/<env>/<sha>.jsonl (runtime) + tracked deploy-evidence/staging/<sha>.jsonl committed by deploy-staging after successful smoke (release gate reads it).
- Smoke: HTTP curl /api/v1/health/live (deploy roles lack elbv2:DescribeTargetHealth — verified in infra/environments/global/main.tf deploy policy).
- SSM provisioning dropped from workflows (roles lack ssm:PutParameter — verified); preflight verifies params exist, fail closed.
- infra-apply/teardown: environment `infra-<env>` (expression), AWS_ROLE_ARN_INFRA_<ENV> secrets (admin-apply), plan artifact flow, teardown hard allowlist dev|staging + typed confirmation teardown-<env>.
- release-service: deploy job implemented in batch 5 (tag parse + evidence + promotion); batch 6 adds production-readiness (required_reviewers check, fail closed), digest assertion, smoke, always() evidence summary.
- Staging env + AWS_ROLE_ARN_STAGING + infra envs + production required-reviewers = admin-apply (recorded in report).
