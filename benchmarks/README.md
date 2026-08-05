# benchmarks/

Performance programs and committed baselines. Benchmarks measure performance
and never masquerade as correctness tests; they are explicit-only jobs in the
test matrix (`scripts/ci/test_matrix.json`), never part of ordinary CI.

| Program | Owner | Run |
|---|---|---|
| `backend/commerce_clustering.py` | backend_service | `uv run python benchmarks/backend/commerce_clustering.py` (uses the production VN embedder; needs model weights) |
| `backend/stage2_pipeline.py` | backend_service | `uv run python benchmarks/backend/stage2_pipeline.py --lane offline [--output ...] [--baseline ...]` |
| `api/latency.py` | backend_service | `uv run python benchmarks/api/latency.py --base https://api.livento.me --token $BK --out .runtime/bench-<ts>.json` |

`baselines/` holds committed baseline JSON (e.g. `stage2/offline.json`) used by
`stage2_pipeline.py --baseline` for p95 regression gating (fail when any
comparable stage p95 is more than 20% slower).

Committed baselines:

| Baseline | What it records |
|---|---|
| `bench-20260713/SUMMARY.md` | 2026-07-13 hardening-stack benchmark (ALB + Fargate Spot mock): health/live ~51 rps @ c20, lite/say e2e p50 1.7s, auth via SSM, TLS RTT ~0.28s Seoul (moved from `.runtime/` in 1.78) |

Programs that need live services or model weights fail loudly when the
dependency is unavailable; they never silently pass.
