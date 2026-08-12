# Tasks: ci-container-build-optimization

## 1. Reusable build workflow cache export

- [x] 1.1 Add `export_cache` boolean input (default `false`) to `_container-build.yml` `workflow_call.inputs` with description "export per-service gha cache even when push is false (PR verification builds)".
- [x] 1.2 Change `cache-to` to run when `export_cache || push` using `format('type=gha,mode=min,scope={0}', inputs.scope)`, empty string otherwise (import-only).
- [x] 1.3 Update the header comment to document that PR verification builds with `export_cache:true` write the shared per-service cache, and deploy builds (`push:true`) behave as before.
- [x] 1.4 Validate `_container-build.yml` YAML parses (`python -c "import yaml; yaml.safe_load(...)"`).

## 2. Main CI workflow gating and export

- [x] 2.1 Add `needs.affected-area.outputs.services_json != '[]'` to the `container-build` `if:` condition in `ci.yml` so docs-only changes skip the build.
- [x] 2.2 Pass `export_cache: true` in the `container-build` `with:` block (all modes remain `push:false`).
- [x] 2.3 Update the ci.yml header comment modes section: docs-only PRs skip container-build; PR verification builds export cache for downstream merge reuse.
- [x] 2.4 Validate `ci.yml` YAML parses.

## 2.5 First-push empty-range fix

- [x] 2.5.1 `detect_changes.py` `changed_paths` handles `base == "0"*40` (first push of a branch, `github.event.before` has no parent) via `git show --name-only --format=` instead of a parentless diff that fails `git diff 0000..sha`.
- [x] 2.5.2 Add `tests/ci/test_detect_changes_first_push.py` covering both `0000...0` (initial commit file list) and normal base..head ranges.

## 3. Verification

- [x] 3.1 Run `uv run python -m pytest tests/ci -q` (repo-tools CI) — workflow-input validation tests pass.
- [x] 3.2 Run `uvx ruff check scripts/ci` — no lint regressions.
- [x] 3.3 Docs-only PR: `container-build` is SKIPPED and `CI / gate` passes. (PR #33 = docs/ci-only; cả 2 runs push+PR: affected-area pass, container-build skipping, gate pass.)
- [x] 3.4 Create a real-code PR (e.g. touch `services/product/tts_service/`) and confirm `container-build` runs, exports cache, and `CI / gate` passes. (PR #35: container-build (tts) pass, log `preparing build cache for export 28.8s` + `sending cache export 36.9s`; gate pass.)
- [x] 3.5 Confirm a follow-up develop/main PR for the same service reuses cached layers (build duration noticeably shorter / cache hit in buildx logs). (PR #36: tts runtime layers `#17/#18 CACHED`, steps DONE 0.0-0.9s; gate pass.)
- [x] 3.6 Run `openspec validate ci-container-build-optimization` — change is valid.
