# Cluster 1.79 report — final cleanup (core/, frontend/, providers/, legacy services, workflows, infra refs)

Status: DONE
Base: 965018b. Branch: worktree-agent-ab41b0dce2704c8a4. 11 commits, 256 files changed (+416/-28968).

## Per-batch results

| Batch | Commit(s) | Targets | Tests | Rollback |
|---|---|---|---|---|
| 1a refs | ad59a8a, 7b2d0df | benchmarks+e2e core imports → canonical; colab launcher → backend.config/llm/tts.engines; ownership-map manifest + KNOWN_MOVED_SOURCES (pre-existing 1.79 gate failure FIXED); benchmarks/backend/fixture_data.py | core 551/3skip, backend 177/1skip+194, llm 25/2skip, tts 26, avatar 89, ci 165, e2e 8, infra 16 | `git revert ad59a8a 7b2d0df` |
| 1b core | dada2c6, 47dd296 | DELETE core/ (136 files); backend Dockerfile COPYs, dockerignores, ci.yml ruff scope + pytest → tests/ci, deploy-dev paths, pyproject testpaths; infra test platform roots → canonical schema | same as above (post-delete rerun) | `git revert dada2c6 47dd296` |
| 2 frontend | 13ddd50 | DELETE frontend/ (2 files); README/docs/notebook workbench refs | docs-only | `git revert 13ddd50` |
| 3 providers | 3e572d7 | DELETE providers/ (20 files); SDK → backend/application/clients/avatar/liveavatar_sdk/; store → tests/sandbox/liveavatar/; classifier legacy branches removed | sandbox 6, backend 177/1skip+194, ci 162, e2e 8 | `git revert 3e572d7` |
| 4 services | ca6d3af | DELETE services/{avatar,backend,llm,tts}/ + .dockerignore + migrate_tests_150.py; deploy-dev/prod llm-tts role → llm+tts; infra test + docs desired_llm_tts/llm-tts refs | ci 162, backend 371/1skip, llm 25/2skip, tts 26, avatar 89, e2e 8, infra 16 | `git revert ca6d3af` |
| 5 docs | d5e2a5c, 67cea14, 9d8f4ee | CLAUDE.md/README/tests-README/runbooks/checklists/architecture/scope + platform READMEs + .claude rules paths + settings.json; engine seams → avatar.engines.windows; avatar src docstrings + platform dockerignores + hub.py | backend 371/1skip, avatar 89, ci 162, e2e 8, infra 16 | `git revert d5e2a5c 67cea14 9d8f4ee` |

## Final grep evidence
- Code-level live imports of core/frontend/providers: ZERO repo-wide (docstring/comment excluded scan).
- Only remaining "core" strings: historical (service-ownership-map manifest rows, service INVENTORY.md migration tables, verify_parity sys.modules aliases — parity-harness design that maps core.director.* → canonical), ScoredCluster symbol.
- Guarded service fallbacks: llm/tts engines/base.py now `try: from avatar.engines.windows import ... except ImportError:` with service-local dataclass fallbacks.

## Route audit (1.25/1.27 gate)
- backend.main:app (create_app): /api/v1/health/live → 200, /api/v1/health → 200, /api/v1/lite/start → 404, /api/v1/debug/status/x → 404, /api/v1/mock/frame/x.png → 404, /api/v1/admin/sandbox/verify → 404. PASS.
- Route inventory tests: test_sandbox_route_absent.py + test_mock_media_absent.py + test_session_routes.py — 8 passed.

## Gate output
- backend unit 177/1skip, backend integration 194, llm 25/2skip, tts 26, avatar 89, tests/ci 162, e2e 8, infra 16, sandbox 6 (LIVEAVATAR_API_KEY=fake) — ALL PASS.
- ruff clean on all touched Python.
- benchmark verify_parity harness: 6/6 fixtures PASS (canonical vs recorded).

## Concerns / notes
- sandbox tests require LIVEAVATAR_API_KEY env (fail loud by design; verified with fake key).
- Multi-service pytest collection in one invocation hits same-basename collisions (documented 1.59; CI runs per-service).
- infra/modules/compute/llm.tf `moved` blocks reference `llm_tts` resource addresses — KEPT (Terraform state migration, not code).
- contracts/v1/, benchmarks/fixtures/, benchmarks/baselines/, .runtime/, uv.lock untouched per constraints.

## Mini-ledger pointer
`.superpowers/sdd/tasks/cluster-1-79-ledger.md`
