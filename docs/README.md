# Docs index — ai-livestream-commerce-vn / implementations

Confirmed architecture + pricing only. Active work plans live in `../plans/`. Historical drafts live in `../archive/docs-historical/`.

## Confirmed design (source of truth)

| File | Role |
|---|---|
| [architecture.md](./architecture.md) | App/control-plane module map (code as-is) |
| [brief-for-confirmation.md](./brief-for-confirmation.md) | Confirmed product/system decisions (engines, Director, Pipecat, LiveKit) |
| [scope-engine-and-models.md](./scope-engine-and-models.md) | LLM / TTS / Avatar / LMCache detail |
| [scope-tts-engines.md](./scope-tts-engines.md) | Short TTS companion (points at scope-engine) |
| [aws-architecture.md](./aws-architecture.md) | AWS Seoul stack, SG matrix, reject list, implement next |
| [terraform-layout.md](./terraform-layout.md) | Root & child modules tree |
| [cicd-branch-strategy.md](./cicd-branch-strategy.md) | Branches + `ci` / `deploy-dev` / `deploy-prod` |
| [aws-pricing-seoul.xlsx](./aws-pricing-seoul.xlsx) | Human pricing (MVP / PROD sheets) |
| [aws-pricing-seoul.csv](./aws-pricing-seoul.csv) | Machine twin |
| [aws-pricing-seoul-validation.md](./aws-pricing-seoul-validation.md) | PASS 45/45 Seoul live check (2026-07-11) |
| [figures/](./figures/) | Architecture diagrams (HTML/PNG) |

## Ops / checklists

| File | Role |
|---|---|
| [runbook-colab.md](./runbook-colab.md) | Colab T4 runbook (dev/demo, not AWS runtime) |
| [checklists/colab-readiness.md](./checklists/colab-readiness.md) | Colab preflight |
| [checklists/release.md](./checklists/release.md) | Pre-merge / release checks |

## Not here

- **Active plans:** `../plans/`
- **Historical (done / superseded):** `../archive/docs-historical/` (`PLAN.md`, `TASKS.md`, `PRODUCTION.md`, `BACKEND_PRODUCTION_FIX_PLAN.md`, old avatar survey)
- **Research notes:** `../notes/`

## Implement next

See `aws-architecture.md` §12 and `../plans/00-implement-aws-stack.md`.
