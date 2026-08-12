# ci-container-build-optimization Proposal

## Why

The merge chain (feature-PR → develop-PR → main-PR) currently runs `container-build` — building all 4 service images (~20-25 min backend) — on **every** PR, including docs-only PRs that cannot affect any Dockerfile, and PR builds never export their cache so downstream develop/main PRs rebuild from scratch. This wastes roughly 6 full image-build rounds per change.

## What Changes

- `container-build` skips when the change is docs-only (no affected product service), matching the existing `service-ci` neutral-skip pattern.
- PR verification builds **export** their per-service cache (`type=gha,mode=min`) so the develop-merge and main-merge builds reuse layers from the feature-PR build instead of rebuilding from scratch.
- Deployment builds (`deploy-dev`, `deploy-staging`, `push:true`) keep exporting as today.
- Full image builds remain for every PR that touches real code (services, infra, shared build/config/locks).

## Capabilities

### New Capabilities

- `ci-container-build-optimization`: CI container-build cache reuse across PR/merge chain + docs-only skip.

### Modified Capabilities

- None — this is a new CI optimization capability; no existing spec changes.

## Dependency and Sequencing

- Independent of Change A/T/B. Branches from current `main`.
- No code-service impact: only `.github/workflows/` changes.

## Impact

- `.github/workflows/_container-build.yml` — new `export_cache` input; `cache-to` runs when `export_cache || push`.
- `.github/workflows/ci.yml` — `container-build` `if:` reads `affected-area.outputs.services_json != '[]'`; passes `export_cache: true`.
- `gate` — unchanged (already tolerates `skipped`).
- Docs-only PRs: `container-build` skipped; code PRs: unchanged behavior plus cache export.
