# ci-container-build-optimization Specification

## ADDED Requirements

### Requirement: Docs-only CI skip
The CI pipeline SHALL skip container image builds for changes that do not affect any product service, matching the existing service test neutral-skip behavior.

#### Scenario: Docs-only pull request
- **GIVEN** a pull request changes only documentation or OpenSpec files
- **WHEN** CI evaluates the change
- **THEN** the `container-build` job SHALL be skipped
- **AND** the `CI / gate` result SHALL remain success

#### Scenario: Code pull request
- **GIVEN** a pull request changes a product service, shared build config, lockfile, or infrastructure
- **WHEN** CI evaluates the change
- **THEN** the `container-build` job SHALL run for the affected images
- **AND** SHALL NOT be skipped solely because the diff is small

### Requirement: PR verification builds export cache
PR verification image builds SHALL export their per-service build cache so later merge builds can reuse layers instead of rebuilding from scratch.

#### Scenario: Feature PR builds an image
- **GIVEN** a feature PR runs `container-build` with `push:false`
- **WHEN** the build completes
- **THEN** its per-service cache SHALL be exported to the shared gha cache scope

#### Scenario: Develop/main merge reuses cache
- **GIVEN** a feature-PR build already exported cache for a service scope
- **WHEN** a later develop or main merge builds the same service
- **THEN** the build SHALL reuse cached layers from the earlier export when the Dockerfile layers are unchanged

### Requirement: Deployment builds keep exporting
Deployment image builds SHALL continue to export cache as today and SHALL NOT be affected by this change.

#### Scenario: Deploy build exports cache
- **GIVEN** a `push:true` deployment build (dev/staging)
- **WHEN** the build completes
- **THEN** it SHALL export cache exactly as before this change

### Requirement: Cache key stability
The container cache key SHALL remain stable per service and SHALL NOT include branch names or commit SHAs.

#### Scenario: Cross-branch reuse
- **GIVEN** builds on different branches for the same service
- **WHEN** each build runs
- **THEN** they SHALL share the same per-service cache scope
- **AND** unchanged layers SHALL be reused across branches
