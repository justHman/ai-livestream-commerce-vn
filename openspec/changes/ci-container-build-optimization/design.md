# Design: ci-container-build-optimization

## Context

`ci.yml` is the branch-governed CI entry (OpenSpec 3.1-3.14). Modes derive from event context, not diff. Every mode ends in one required-check result `CI / gate`, and skipped unaffected-area jobs report neutral success so branch protection never waits on jobs not meant to run (design §3.3).

`_container-build.yml` already keys cache by **per-service gha scope** (`type=gha,scope=<service>`), deliberately excluding branch/SHA so cross-branch reuse works (design §2). The gap:

1. `container-build`'s `if:` (ci.yml:189) reads only `mode` — never `affected-area.outputs.services_json` — so docs-only PRs still build all 4 images.
2. `push:false` PR builds do not export the cache; only `push:true` deploy builds write it. Develop/main PRs therefore rebuild layers the feature-PR already built.

## Goals

1. Docs-only PRs skip `container-build` (docs/, notes/, openspec/ → neutral → `services_json == '[]'`, guaranteed by `detect_affected_areas.py:153`).
2. Feature-PR builds export their per-service cache so develop-merge/main-merge reuse layers (cache key already stable — no key change needed).
3. Keep full builds for any PR touching real code.

## Non-goals

- No cache-key redesign (scope already correct per design §2).
- No `push:true` changes in deploy workflows.
- No service-ci/workbench/platform/terraform/repo-tools changes.

## Design

### `_container-build.yml`

Add input `export_cache` (boolean, default `false`) — decouples "export cache" from "push image".

```yaml
on.workflow_call.inputs.export_cache:
  type: boolean
  required: true
```

`cache-to` runs when `export_cache || push`, else empty (import-only):

```yaml
cache-to: ${{ (inputs.export_cache || inputs.push) && format('type=gha,mode=min,scope={0}', inputs.scope) || '' }}
```

Buildx ignores an empty `cache-to` (import-only). `mode=min` kept (final-layer export — the existing trade-off; `mode=max` costs 10+ min on torch deps).

Update header comment: PR verification with `export_cache:true` writes the shared per-service cache; deploy builds (`push:true`) unchanged.

### `ci.yml`

**`container-build` `if:`** — add docs-only skip:

```yaml
if: ${{ always() && needs.mode.result == 'success' && needs.affected-area.outputs.services_json != '[]' && (needs.mode.outputs.name == 'feature-pr' || needs.mode.outputs.name == 'develop-merge' || needs.mode.outputs.name == 'release-pr' || needs.mode.outputs.name == 'main-merge') }}
```

`affected-area` is already a `needs` dependency (line 188), so this is a read-only addition.

**`with:`** — pass `export_cache: true` in every mode (all modes are `push:false` verification builds):

```yaml
with:
  ...
  export_cache: true
  push: false
```

### `gate`

No change: `CTN` already accepts `skipped` in the aggregation loop (line 170). A skipped `container-build` reports `skipped` and passes.

### Security note

`export_cache:true` is set only in `ci.yml` (branch-governed). Untrusted fork PRs cannot reach a deployment build (repo is user-owned, branch protection enforces required checks). Optional hardening if forks ever become a concern: gate `export_cache` on `github.event.pull_request.head.repo.full_name == github.repository`.

## Cache-reuse flow after change

```text
feature-PR (export_cache:true) ──writes gha cache scope=<service>──▶
develop-PR (import same scope) ──layer hits, fast──▶
main-PR   (import same scope)  ──layer hits, fast──▶
deploy-dev/staging (push:true) ──reuses + re-exports──▶
```

Docs-only PR: `services_json == '[]'` → `container-build` skipped, `service-ci` skipped, `gate` success.

## Decisions

- **Export from PR builds**: accepted — `mode=min` limits write cost; branch protection prevents untrusted writes.
- **Skip condition on `services_json != '[]'`**: matches the existing `SVC` normalization pattern in `gate` (ci.yml:161); root-config-only changes (`uv.lock`, `Dockerfile` at root) classify as shared areas → still build (defensive, correct).

## Migration / risk

- Existing PRs: no behavior regression — code PRs build as before, plus cache export.
- Fork PRs: blocked by branch protection; optional `head.repo` guard noted above.
- Verify with a docs-only PR (skip) and a real-code PR (build + export + reuse).
