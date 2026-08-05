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
