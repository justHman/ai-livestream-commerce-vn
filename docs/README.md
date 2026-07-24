# Documentation index

These documents separate implemented, offline-validated code from design intent
and unverified external operations. Active execution plans are in `../plans/`.

## Current technical reference

| File | Scope |
|---|---|
| [architecture.md](./architecture.md) | Current FastAPI control plane, routes, lifecycle, and LiveKit boundary |
| [aws-architecture.md](./aws-architecture.md) | AWS contract and current offline-only deployment state |
| [terraform-layout.md](./terraform-layout.md) | Terraform roots, bootstrap, state, and Tier S profile |
| [cicd-branch-strategy.md](./cicd-branch-strategy.md) | Actual CI, DEV, and manual PROD workflow behavior |
| [scope-engine-and-models.md](./scope-engine-and-models.md) | Target engine and model design |
| [brief-for-confirmation.md](./brief-for-confirmation.md) | Confirmed product decisions, not an execution-status ledger |
| [aws-pricing-seoul.csv](./aws-pricing-seoul.csv) | Machine-readable Seoul estimate |

## Operations

| File | Scope |
|---|---|
| [runbook-colab.md](./runbook-colab.md) | Colab vLLM demo only |
| [runbook-deploy-prep.md](./runbook-deploy-prep.md) | Offline AWS deployment preparation; no apply |
| [runbook-live-smoke-and-teardown.md](./runbook-live-smoke-and-teardown.md) | Approved Tier S apply, smoke capture, and teardown |
| [SHIP-CHECKLIST-DEPLOY-PREP.md](./SHIP-CHECKLIST-DEPLOY-PREP.md) | M4 readiness and external gates |
| [checklists/release.md](./checklists/release.md) | Verification before merge or release |

## Boundaries

- Offline tests and Terraform validation do not prove AWS, LiveKit media,
  Docker Hub, DNS, or a release.
- Tier S runs one mock API backend with GPU, avatar, LiveKit, and LMCache
  desired counts at zero. It is billable and requires confirmation immediately
  before execution.
- Historical documents remain outside this active documentation surface.
