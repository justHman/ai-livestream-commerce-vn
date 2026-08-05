# Cluster 6.x report — final verification + migration (6.1-6.6)

Status: DONE_WITH_CONCERNS
Base: c635f4f (refactor branch tip). Worktree: agent-a3a73b0850ff83718. 7 commits.
Mini-ledger: `.superpowers/sdd/tasks/cluster-6x-ledger.md`.

## Per-task verification + evidence

| Task | Commit(s) | What changed | Verification |
|---|---|---|---|
| 6.1 CI modes never deploy | cd51c25 | tests/ci/test_detect_affected_areas.py: test_ci_event_modes_never_deploy (ci.yml trigger set = push/PR only; mutation all-false; 5 modes pinned), test_ci_gate_job_aggregates_all_modes (gate deps) | tests pass; ci.yml mutation = {artifact_push:false, deploy:false, infra_mutation:false}; no dispatch/schedule/tag; static validator R4/R9 PASS |
| 6.2 gh CLI vs web/REST parity | 891e174 | docs/deploy-commands.md: input-equivalence section (gh CLI / web UI / REST = same workflow_dispatch payload; same commit_sha + services validation; thin wrappers) | wrapper arg validation simulated; profile binding deploy-dev→dev / deploy-staging→staging; rejection paths (bad SHA, unknown service, injection) verified via validate_workflow_inputs; real dispatch attempt vs remote OLD deploy-dev = HTTP 422 (no dispatch trigger — refactor branch not merged); bash -n OK |
| 6.3 release gates | 6a9e714 | scripts/ci/simulate_release_path.py + tests/ci test_release_simulation_all_gates_pass | 5.1 tag parse (backend-v1.2.3 ok; backend-v1.2/v1.2.3/database-v1.2.3/lmcache-v1.2.3/'' rejected), 5.2 main ancestry + staging evidence gate, 5.4 exact-digest promotion (immutable @sha256, no rebuild), 5.5 service-scoped rollback (update-service to old task def, per ECS_SERVICE) — ALL GATES PASS |
| 6.4 disable superseded triggers | 5912e8a | deploy-prod.yml: `on: {}` (push tags v* + workflow_dispatch removed) with full disable-record comment; docs updated (cicd-branch-strategy, workflow-graph-audit); tests updated (test_deploy_prod_triggers_disabled, service_tags==[]) | build-images.yml NOT disabled — verified actual usage first: 20 historical runs, exercised by infra/tests/test_platform_roots.py, artifact_push only (never deploy), documented offline prep path; deploy-prod ZERO runs in remote history, no active refs; revertible (file retained, one commit); YAML parse on:{} OK, static validator 12/12 PASS |
| 6.5 workflow graph audit | cd51c25 | docs/workflow-graph-audit.md | 12-workflow graph vs event-to-action matrix (8 event-entry + 4 reusable); 7 findings (no implicit deploy, fan-out, stable gate, reusable isolation, explicit-only deploy, digest-exact prod, build-images kept); admin-side residuals listed |
| 6.6 terraform validation | de76f8f | docs/infra-validation-6x.md evidence; stale llm_engine=openai_compat comments removed (dev/main.tf, compute/backend.tf) | fmt -check -recursive PASS; init -backend=false + validate PASS for global/dev/staging/prod; terraform native tests 4/4 PASS (runtime_matrix.tftest.hcl: zero-cost hosted, independent llm/tts, no-forbidden-topology, digest+circuit-breaker); pytest infra/tests 16 PASS; boundary check OK (8 modules, lockfiles, no DynamoDB, no workspaces, immutable digests, Cloud Map wiring verified: discovery.tf + registry_arns + LLM_BASE_URL/TTS_BASE_URL default DNS) |

## Workflow graph audit table (6.5, summarized)

| Workflow | Trigger | Deploy? | Notes |
|---|---|---|---|
| ci.yml | push feature/**/develop/main, PR→develop/main | No | 5 modes, secret-scan + gate |
| _python-service-ci.yml | workflow_call | No | per-service checks |
| _container-build.yml | workflow_call | No | push only when caller sets push:true |
| _deploy-service.yml | workflow_call | Deploy (caller-owned) | rollback + smoke, no env |
| deploy-dev.yml | dispatch | Dev | commit/SHA/CI gate validation |
| deploy-staging.yml | dispatch | Staging | evidence commit to main |
| release-service.yml | push tag *-v* | Prod | staging evidence + digest promotion |
| infra-apply.yml | dispatch | Infra dev/staging | protected plan→apply |
| infra-teardown-nonprod.yml | dispatch | Infra dev/staging | hard allowlist |
| build-images.yml | dispatch | No | offline image prep, kept active |
| seed-weights.yml | dispatch | No | HF→S3 |
| deploy-prod.yml | DISABLED (on: {}) | — | superseded by release-service; revertible |

## Disabled triggers (6.4)

- deploy-prod.yml `on.push.tags: ["v*"]` and `on.workflow_dispatch` — removed
  (deliberate, documented, revertible; file retained with `on: {}`).

## Terraform validation results per env (6.6)

| Env | fmt | init | validate | native test |
|---|---|---|---|---|
| global | PASS | PASS | PASS | n/a |
| dev | PASS | PASS | PASS | — |
| staging | PASS | PASS | PASS | — |
| prod | PASS | PASS | PASS | — |
| infra/tests | — | PASS | — | 4/4 PASS |

## Admin-apply list (GitHub/AWS-side, cannot run locally — NOT blocking)

1. Merge/land the refactor branch on `develop`/`main` (remote main is at
   e41dc7f; release-service/deploy-staging/_deploy-service/infra-* workflows
   and dispatch-only deploy-dev are not on the remote yet).
2. Apply rulesets `develop-protection` + `main-protection` (JSON in
   `.github/rulesets/`; repo currently has ZERO rulesets via API).
3. Create GitHub Environments: `staging`, `infra-dev`, `infra-staging`
   (required reviewers), configure `production` required-reviewers.
4. Secrets: AWS_ROLE_ARN_STAGING, AWS_ROLE_ARN_INFRA_DEV/STAGING;
   provision SSM /dev/*, /staging/* api_tokens out-of-band.
5. Live drills (6.6): non-production infra apply/teardown, immutable-digest
   rollout + circuit-breaker rollback, Cloud Map DNS resolution.
6. Secret Scanning / Push Protection enablement (docs/secret-scanning.md).

## Commit SHAs

ec2dbc1 (ledger) → cd51c25 (6.1+6.5) → 66ccba3 (ledger tick) → 891e174 (6.2)
→ 6a9e714 (6.3) → 5912e8a (6.4) → de76f8f (6.6)

## Mini-ledger pointer

`.superpowers/sdd/tasks/cluster-6x-ledger.md`

## Concerns

- 2 PRE-EXISTING test failures in tests/ci/test_inventory_workflows.py
  (test_every_workflow_has_canonical_target, test_step_uses_captured) exist
  at base c635f4f — NOT caused by this cluster (1.59/6.x test ownership).
- Remote main is stale (e41dc7f): none of the 4.x/5.x/6.x workflow changes
  are live until the refactor branch merges; 6.2's real-dispatch verification
  is therefore only locally simulated + the HTTP 422 proof.
- 6.6 live drills (apply/teardown, digest rollout, Cloud Map) require AWS
  credentials — recorded as admin-apply, not run.
- actionlint not available (YAML parse + static validator used).
