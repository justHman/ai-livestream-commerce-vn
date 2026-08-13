## MODIFIED Requirements

### Requirement: Realtime runtime configuration controls
The console SHALL expose validated controls for fast-lane microbatch maximum wait, rolling demand horizon, reconciliation count threshold, reconciliation age threshold, Q&A eligibility/cooldown, Agent execution budgets, bounded memory limits, evidence freshness/prefetch policy where operator-tunable, prepared-turn depth where still applicable, retry limits, demand-pivot thresholds, and speech-arbitration tuning that does not weaken the active-sentence non-preemption invariant. The former `initial_ingest_mode` control is removed because canonical `/events` accepts one or many events through one implementation path. An accepted update SHALL apply from the next safe runtime boundary without resetting opening, approved-script cursor, or the current product checkpoint; stale prepared work SHALL be invalidated by revision token.

#### Scenario: Rolling horizon changes independently
- **WHEN** the operator changes rolling demand horizon
- **THEN** microbatch maximum wait and reconciliation age SHALL remain unchanged unless separately edited
- **AND** the config revision SHALL be reflected in subsequent diagnostics.

#### Scenario: Agent budget is invalid
- **WHEN** the operator submits an unsupported or unbounded evidence-round budget
- **THEN** the backend SHALL reject the configuration with a typed validation error.

#### Scenario: Arbitration threshold changes during sentence playback
- **WHEN** the operator changes pending-Q&A priority/hysteresis while an approved script sentence is speaking
- **THEN** the active sentence SHALL still complete normally
- **AND** the new configuration SHALL apply to subsequent boundary arbitration.

#### Scenario: Removed ingest-mode field is submitted
- **WHEN** a client submits `initial_ingest_mode` after this change
- **THEN** it SHALL not be part of the canonical runtime configuration contract.
