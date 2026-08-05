# tests/ — test matrix and repository-level suites

## Approved test matrix

The machine-readable definition lives in `scripts/ci/test_matrix.json`
(consumed by CI workflows in cluster 3.x). Affected-service detection is
`scripts/ci/detect_changes.py` (wraps `scripts/ci/detect_affected_areas.py`).

| Trigger | Runs | Coverage gate | Extra |
|---|---|---|---|
| Feature push | Affected service unit tests only | — | No integration/contract/E2E/sandbox/GPU/benchmark |
| Pull request | Affected unit + integration + contract | `--cov-fail-under=80` branch | Cached container validation |
| Merge commit | Same as PR, on the exact merge commit | `--cov-fail-under=80` branch | Cached container validation |
| Release verification | Affected cross-service E2E (`tests/e2e/`) | — | Requires live stack; fails loudly when unavailable |
| Explicit only | Sandbox (`tests/sandbox/`), hosted provider, GPU, benchmark (`benchmarks/`) | — | Authorized/staging/manual jobs only |

## Layout

- `tests/ci/` — repository-tool tests (affected-area classifier, workflow
  validation). Collected by ordinary root runs.
- `tests/e2e/` — cross-service stack behavior; NOT collected by ordinary runs
  (`norecursedirs` in root `pyproject.toml`). Select explicitly.
- `tests/sandbox/` — real hosted-provider checks; NOT collected by ordinary
  runs. Missing credentials fail loudly, never skip.
- `core/tests/` — legacy compatibility suite (core/ is frozen pending removal).

## Per-service suites

Each product service keeps its own `tests/{unit,integration,contract}/` under
`services/product/*_service/`. The service's `pyproject.toml` pytest config
ignores `tests/integration` and `tests/contract` in the ordinary (feature-push)
path; those tiers run only on the PR/merge path. Coverage is enforced per
service with `[tool.coverage.report] fail_under = 80` and branch coverage.

## Using the detector

```bash
# Affected services for the last 3 commits (plain + JSON)
uv run python scripts/ci/detect_changes.py --range HEAD~3..HEAD
uv run python scripts/ci/detect_changes.py --range HEAD~3..HEAD --json

# Affected services between two refs
uv run python scripts/ci/detect_changes.py main...develop --json
```

Output: `{"services": [...], "areas": [...], "by_path": {...}}`. `services` is
the product-service subset (`backend_service`, `llm_service`, `tts_service`,
`avatar_service`); contract/source-DTO changes fan out to exact consumers.
