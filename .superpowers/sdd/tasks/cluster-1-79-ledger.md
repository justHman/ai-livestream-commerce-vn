# Cluster 1.79 ledger — final cleanup (core/, frontend/, providers/, legacy services, workflows, infra refs)

Base: 965018b (supervisor HEAD). Worktree: agent-ab41b0dce2704c8a4.

## Audit (pre-dispatch, supervisor)

### Live core imports (real code, docstring/comment excluded)
- benchmarks/backend/{commerce_clustering,stage2_pipeline}.py — module-level `from core.*`
- benchmarks/fixtures/{corpus,products}.py + scenarios/*.py — module-level `from core.director.*`, `core.schemas.*`
- benchmarks/baselines/__main__.py — function-level `from core.director.*`
- benchmarks/verify_parity.py — sys.modules aliases `core.director.*` → canonical (by design, parity harness)
- providers/liveavatar_cloud/examples/colab_deploy.py — function-level `from core.config/llm/tts/render`
- tests/e2e/test_benchmark_contracts.py — module-level `from core.director.director`, `core.render.orchestrator`, `core.render.base`
- services/product/llm_service/src/llm/engines/base.py:34 — guarded `try: from core.render.windows import TextChunk` (service-local fallback OK)
- services/product/tts_service/src/tts/engines/base.py:20 — guarded `try: from core.render.windows import ...` (service-local fallback OK)
- services/product/llm_service/src/llm/engines/{llamacpp,vllm}.py:10/15 — DOCSTRING-only, no real import

### Zero real core imports in canonical service src
- backend_service/src (all 130+ files), avatar_service/src, llm_service/src (except guarded fallbacks above), tts_service/src (except guarded fallback above)

### Route audit (canonical app = backend.main:app)
- backend/api/v1/{router,sessions,admin,__init__}.py — production contract: no /lite/*, /debug/*, /mock/*, sandbox routes
- Route inventory tests present: tests/integration/test_sandbox_route_absent.py (404 asserts), test_mock_media_absent.py (404), test_session_routes.py::test_mock_routes_404_in_prod_without_debug
- /lite/*, /debug/*, /mock/*, /admin/sandbox/verify routes exist ONLY in core/api/v1/* (being deleted)

### Ownership map test (pre-existing 1.79 blocker)
- core/tests/test_service_ownership_map.py — KNOWN_MOVED_SOURCES stale: manifest has 12 missing sources
  (core/server.py, services/livekit/, services/lmcache/, scripts/bench_*.py, scripts/stage_smoke.ps1,
  scripts/teardown_verify.ps1, scripts/swap_task_image.py, scripts/gen_vllm_modelinfo_cache.py,
  scripts/upload_weights_s3.py, services/scripts/fetch_weights.sh)
- Test asserts all manifest sources exist OR in KNOWN_MOVED_SOURCES → currently fails (1 fail)
- Plan: fix manifest sources to canonical targets + update KNOWN_MOVED_SOURCES, then delete test with core/

### Deletion targets
| Target | Tracked files | Notes |
|---|---|---|
| core/ | 136 | frozen; deleted wholesale at batch 1 |
| frontend/ | 2 (index.html, lite.html) | superseded by workbench/ |
| providers/ | 20 | liveavatar_cloud; SDK used by backend liveavatar.py client + sandbox tests |
| services/{avatar,backend,llm,tts}/ | 13 | legacy Dockerfiles; superseded by services/product/*_service |
| scripts/migrate_tests_150.py | 1 | one-shot (1.50), no refs |
| services/README.md llm-tts row | — | doc fix |
| infra/.tf comments (llm_tts moved blocks, cluster.tf) | — | KEEP moved blocks (state migration), fix stale comments |
| .github/workflows/{ci,deploy-dev}.yml | — | drop core/providers/frontend paths + ruff scope + core/tests run |
| pyproject.toml testpaths | — | drop core/tests |
| docs (architecture, runbooks, checklists) | — | drop core/frontend/providers references |
| .dockerignore frontend/ + backend Dockerfile core/providers COPY + Dockerfile.dockerignore | — | remove |
| infra/tests/test_platform_roots.py | — | drop core/sql assertion (canonical db/sql identical) |
| docs/service-ownership-map.md manifest | — | remap legacy sources → canonical targets (or KNOWN_MOVED) |

### Preserve (DO NOT TOUCH)
- contracts/v1/, benchmarks/fixtures/, benchmarks/baselines/, .runtime/, uv.lock, archived/
- services/platform/*, services/product/*_service/*, workbench/, infra/modules/* tf moved blocks

## Execution (supervisor + implementer model, worktree agent-ab41b0dce2704c8a4)

### Batch 0 — ledger + audit baseline
- 7c52948 chore(sdd): 1.79 cluster ledger (audit baseline)

### Batch 1 — reference fixes + core/ deletion (the big one)
- ad59a8a refactor(benchmarks): benchmark/e2e imports → canonical services (backend.*, llm.*, tts.*, avatar.*); ownership-map manifest + test KNOWN_MOVED_SOURCES updated (pre-existing 1.79 gate failure fixed); new benchmarks/backend/fixture_data.py (workbench JSON + 4 stage2 texts)
- 7b2d0df refactor(colab): colab_deploy.py uses backend.config + llm/tts.engines (was core.config/llm/tts/render)
- dada2c6 chore(core): DELETE core/ (136 files) + backend Dockerfile/dockerignores + ci.yml (ruff scope, pytest tests/ci) + deploy-dev paths + pyproject testpaths
- 47dd296 fix(tests): infra/tests/test_platform_roots.py reads canonical db/sql/runtime_schema.sql
- Verify: core suite 551 pass/3 skip (pre-deletion), backend unit 177/1skip + integration 194 pass, llm 25/2skip, tts 26, avatar 89, tests/ci 165, e2e 8, infra 16; route gate (TestClient): /lite /debug /mock /sandbox all 404, health 200

### Batch 2 — frontend/ deletion
- 13ddd50 chore(frontend): DELETE frontend/ (index.html, lite.html) + README/docs/notebook workbench refs
- Verify: no frontend refs remain in live docs

### Batch 3 — providers/ deletion
- 3e572d7 chore(providers): DELETE providers/ (20 files). SDK moved → backend/application/clients/avatar/liveavatar_sdk/ (client, audio, conversation, lite_agent); store → tests/sandbox/liveavatar/; sandbox tests + backend liveavatar.py re-pointed; detect_affected_areas classifier legacy branches removed (core/frontend/providers + core schema fan-out)
- Verify: sandbox 6 pass (with fake key), backend 177/1skip + 194, tests/ci 162, e2e 8

### Batch 4 — legacy service dirs + workflows + infra refs
- ca6d3af chore(services): DELETE services/{avatar,backend,llm,tts}/ + services/.dockerignore + scripts/migrate_tests_150.py; split llm-tts deploy role → llm + tts in deploy-dev.yml + deploy-prod.yml (terraform outputs now llm/tts separate); infra test llm_tts → llm; docs desired_llm_tts → desired_llm/desired_tts; cluster.tf/compute README/swap_task_image/pricing csv/architecture html llm-tts refs
- Verify: tests/ci 162, backend 371/1skip, llm 25/2skip, tts 26, avatar 89, e2e 8, infra 16

### Batch 5 — docs/rules/settings/platform cleanup
- d5e2a5c docs(cleanup): CLAUDE.md/README/tests-README/SHIP-CHECKLIST/runbooks/plan-review/cicd/scope-engine/architecture/checklists + services platform READMEs + validate_config + backend_service README + .claude rules paths + settings.json (core.tests.* permission removed)
- 67cea14 fix(llm,tts): engine seams import avatar.engines.windows (was guarded core.render.windows fallback); docstring core refs removed
- 9d8f4ee chore(cleanup): avatar src docstrings + platform dockerignores frontend/ + hub.py stale core ref
- Verify: backend 371/1skip, avatar 89, tests/ci 162, e2e 8, infra 16

## FINAL GATE
- Live legacy imports (code-level, docstring/comment excluded): ZERO core/frontend/providers imports repo-wide
- Only remaining "core" strings: historical docs (service-ownership-map manifest, INVENTORY.md migration tables, verify_parity sys.modules aliases — parity harness design), ScoredCluster (unrelated)
- Route audit (backend.main:app): /api/v1/health/live 200, /health 200, /lite/start 404, /debug 404, /mock 404, /admin/sandbox/verify 404 — PASS
- Full gate: backend 177/1skip + 194, llm 25/2skip, tts 26, avatar 89, tests/ci 162, e2e 8, infra 16, sandbox 6 (fake key) — ALL PASS
- ruff clean on touched Python
- Rollback: each batch = `git revert <commit>` (per-batch commits listed above)
