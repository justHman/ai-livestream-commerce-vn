# Benchmark 2026-07-13 — hardening stack (API-only, mock)

## Stack
- ALB HTTPS:443 + HTTP:80→301 (ACM api.livento.me ISSUED)
- Backend Fargate Spot ARM64 x1 (1024 CPU / 2048 MiB), mock/tone/memory
- Tokens via SSM SecureString (BACKEND_API_TOKEN, ADMIN_API_TOKEN)
- Endpoint: https://api.livento.me

## health/live (GET, no auth)
| conc | N | rps | ok | p50 | p95 | p99 |
|------|---|-----|----|----|----|-----|
| 1  | 100 | 3.3  | 100 | 283ms | 359ms | 1003ms |
| 5  | 100 | 16.4 | 100 | 289ms | 387ms | 826ms |
| 20 | 100 | 51.0 | 100 | 317ms | 383ms | 443ms |

Latency floor ~280ms is TLS+RTT Seoul from this box (not backend CPU).

## lite/say (POST, viewer token, mock pipeline)
| conc | N | ok | rps | e2e p50 | e2e p95 | pipeline_total_ms |
|------|---|----|-----|---------|---------|-------------------|
| 5 | 30 | 24/30 | 3.1 | 1697ms | 2345ms | avg 822 / p50 810 / p95 1204 |

6/30 → 503 (single-task concurrency cap + per-session lock contention under c5).
Mock pipeline internal latency ~810ms p50.

## Findings (prod-readiness)
1. Single-task (1 vCPU/2GB) sustains ~51 rps on health, but lite/say at c5 drops (503) → scale-out needed for real load (desired_backend > 1).
2. Mock pipeline ~0.8s is the rendering/synthesis stub floor, not network.
3. Auth via SSM works (401 → 200 after SSM token fix + force redeploy).
4. TLS RTT from this box ~0.28s; clients near Seoul will see lower.

## Recommended next
- Auto-scaling backend desired 1→2→4 by ALB RPS for prod
- Redis SESSION_STORE (not memory) before multi-instance (P4)
- Real LLM/TTS engines for true pipeline benchmark (GPU tier)

## Teardown
- terraform destroy -var-file=terraform.tfvars → 0 residual dev billable
- Kept: ACM cert (free), S3 tfstate, DDB lock, OIDC, CF DNS records
