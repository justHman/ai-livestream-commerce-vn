# Cluster 6.x ledger — final verification + migration (CI no-deploy, gh CLI parity, release simulation, trigger disable, terraform validation)

Base: c635f4f (refactor branch tip). Worktree: agent-a3a73b0850ff83718.
Brief: launch message (task-6x-supervisor-brief.md missing from worktree; prior-cluster pattern followed).
Design: §1 branch-governed path, §4 event-to-action matrix, §5 release, §6 rollback; Migration Plan step 12.
Constraints: no touch services/*/src/, contracts/v1/, benchmarks/, uv.lock, .runtime/; no Co-Authored-By; 6.4 disable = deliberate documented revertible commit; admin-only items → report.

## Batch plan (≤2 tasks, one commit each)
- B1: 6.1 (CI modes vs matrix — no deploy in ci.yml) + 6.5 (workflow graph audit report)
- B2: 6.2 (gh CLI wrappers vs workflow inputs + local validation)
- B3: 6.3 (release tag parsing + digest promotion + rollback — local simulation)
- B4: 6.4 (disable superseded triggers: deploy-prod.yml + check build-images.yml) — ONLY after B1-B3 dry-runs pass
- B5: 6.6 (terraform fmt/init/validate per env + native tests + immutability/CloudMap/secret checks)

## Execution

### Batch 0 — ledger + audit baseline
- DONE ec2dbc1 chore(sdd): 6x cluster ledger (audit baseline, batch 0)

### Batch 1 — 6.1 + 6.5
- DONE cd51c25 test(ci): 6.1 no-deploy mode guards + 6.5 workflow graph audit doc
- 6.1: tests/ci/test_detect_affected_areas.py + test_ci_event_modes_never_deploy + test_ci_gate_job_aggregates_all_modes (ci.yml trigger set, mutation all-false, gate deps).
- 6.5: docs/workflow-graph-audit.md — 12-workflow graph vs event-to-action matrix, audit findings, admin-side residual items.
- Verified: tests 164 pass (2 pre-existing base failures), static validator 12/12 PASS.

### Batch 2 — 6.2
- DONE (next commit): wrappers verified against workflow inputs; deploy-commands.md documents input equivalence (gh CLI / web UI / REST).
- Real dispatch attempt vs remote OLD deploy-dev: HTTP 422 (no workflow_dispatch trigger) — proves refactor branch not yet merged; wrappers target NEW inputs.
- Verified: validate_workflow_inputs profile binding + rejection paths simulated locally; bash -n OK; tests 164 pass (2 pre-existing).

### Batch 3 — 6.3
- DONE (next commit): scripts/ci/simulate_release_path.py + tests/ci test_release_simulation_all_gates_pass.
- Local simulation of 5.1 tag parse (eligible + ineligible), 5.2 main ancestry + staging evidence gate, 5.4 exact-digest promotion (immutable, no rebuild), 5.5 service-scoped rollback. ALL GATES PASS.
- Verified: pytest 43 pass in file; bash -n not needed (python harness).

### Batch 4 — 6.4
- PENDING

### Batch 5 — 6.6
- PENDING

## Key decisions (supervisor)
- 6.4 disabled triggers: deploy-prod.yml `on.push.tags v*` (superseded by release-service.yml `*-v*`) + `on.workflow_dispatch` (manual prod deploy path superseded); build-images.yml → kept but restricted (workflow_dispatch retained, see report).
- Local simulation for 6.3: python harness runs validate_service_tag + validate_workflow_inputs + evidence parse against fixture JSONL; bash -n on workflow scripts.
- 6.6 verification: terraform fmt -check -recursive infra; per-env init -backend=false + validate; terraform test (native, infra/tests) via tf env; static script checks (immutability digests, Cloud Map wiring, secret/state).
