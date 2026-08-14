# persistent-live-demand-reducer Specification

## Purpose

Event-driven persistent live-demand reduction for a commerce livestream: an event-driven fast lane (no unconditional fixed polling), an independent rolling demand horizon, count/age reconciliation triggers, a bounded stable per-session ClusterStore with stable cluster identities, soft product routing with multi-product resolution, unique-viewer-aware ranking, semantic representatives, and a compact ClusterEnvelope as the only viewer-demand boundary into the Agentic Director.

## Requirements

### Requirement: Event-driven fast reducer
Accepted semantic comments SHALL wake an event-driven reducer; the runtime SHALL NOT require unconditional fixed-interval polling to discover work.

#### Scenario: Session is idle
- **WHEN** no semantic event or scheduling deadline is pending
- **THEN** the reducer SHOULD remain asleep
- **AND** it SHALL NOT wake solely because the historical 300 ms tick elapsed.

#### Scenario: Burst arrives
- **WHEN** multiple accepted comments arrive within `microbatch_max_wait_ms`
- **THEN** they MAY be coalesced into one embedding batch
- **AND** the configured wait SHALL be a maximum coalescing delay rather than a periodic tick.

### Requirement: Rolling demand horizon is independent of batching
Active demand SHALL be computed over a configurable rolling horizon, initially retaining a 75-second default, independent of ingestion batch timing.

#### Scenario: Old demand expires
- **WHEN** a cluster has no active member within the rolling horizon
- **THEN** its old members SHALL no longer contribute to active ranking/pivot demand
- **AND** bounded cleanup MAY evict state no longer required for reconciliation/audit.

### Requirement: Reconciliation triggers on count or age
Each session SHALL run heavier cluster reconciliation when either 100 accepted unreconciled semantic comments accumulate or 60 seconds elapse since the first unreconciled comment, whichever occurs first.

#### Scenario: High traffic
- **WHEN** the 100th unreconciled comment arrives after 12 seconds
- **THEN** reconciliation SHALL become eligible immediately
- **AND** the runtime SHALL NOT wait for 60 seconds.

#### Scenario: Low traffic
- **WHEN** only 14 unreconciled comments arrive and 60 seconds elapse since the first
- **THEN** reconciliation SHALL become eligible
- **AND** those comments SHALL NOT have been withheld from the fast lane while waiting.

### Requirement: Stable bounded cluster identities
The reducer SHALL maintain stable cluster objects with stable IDs across fast-lane updates and Director decisions.

#### Scenario: Same topic gains new demand
- **WHEN** new comments join an existing semantic topic
- **THEN** the active cluster SHALL retain its `cluster_id`
- **AND** lifecycle state such as last-selected/last-answered/skip state SHALL remain associated with that stable cluster.

### Requirement: Reconciliation repairs active cluster quality
Reconciliation SHALL repair active cluster quality; it MAY merge compatible clusters, split low-cohesion clusters, recompute centroid/medoid/representatives, repair ambiguous routing, and remove expired/duplicate demand over the bounded active horizon.

#### Scenario: Arrival order caused two microclusters
- **WHEN** reconciliation determines two active clusters represent one compatible topic
- **THEN** they MAY be deterministically merged
- **AND** member identities/demand SHALL not be duplicated.

### Requirement: Reducer memory remains bounded
The runtime SHALL prune active comment/embedding/member state that is no longer required by the rolling horizon, reconciliation, or configured diagnostics.

#### Scenario: Six-hour synthetic livestream
- **WHEN** a deterministic long-duration fixture produces continuous traffic
- **THEN** reducer memory SHALL remain bounded by configured active-state limits
- **AND** it SHALL not grow linearly with total historical comment count.

### Requirement: Soft product routing
Pre-cluster routing SHALL produce candidate evidence and SHALL not permanently force every comment into one product partition when confidence is weak.

#### Scenario: Ambiguous reference
- **WHEN** a viewer says "cái này pin tốt không?" and no explicit product is identifiable
- **THEN** the comment MAY remain product-ambiguous
- **AND** cluster-level resolution or Agent evidence reasoning MAY resolve it later.

### Requirement: Multi-product clusters
The reducer SHALL represent clusters that legitimately reference multiple products.

#### Scenario: Comparison question
- **WHEN** viewers ask whether P001 or P020 is more suitable
- **THEN** the cluster SHALL be able to resolve both products
- **AND** the runtime SHALL NOT discard the comparison merely because one hard product partition is required.

### Requirement: Unique-viewer-aware ranking
Actionable ranking and pivot demand SHALL treat unique-viewer demand as a first-class signal and message count as a separate secondary signal.

#### Scenario: Spam repetition versus broad demand
- **GIVEN** one cluster has twenty repeated messages from one viewer
- **AND** another has six equivalent messages from six viewers
- **WHEN** demand popularity is evaluated
- **THEN** the first cluster SHALL NOT automatically dominate solely because twenty raw messages exceed six.

### Requirement: Semantic representatives
Agent-facing representatives SHALL be selected semantically rather than by arrival order.

#### Scenario: Large cluster
- **WHEN** a cluster contains many paraphrases
- **THEN** the reducer SHALL include a medoid and bounded diversity representatives
- **AND** the Agent SHALL not require the entire member list.

### Requirement: Compact ClusterEnvelope boundary
The reducer SHALL emit a compact selected `ClusterEnvelope` containing the information required for reactive Q&A.

#### Scenario: Agent receives demand
- **WHEN** a cluster wins Q&A selection
- **THEN** the Agent SHALL receive its envelope with representative questions, product candidates/resolution, unique-viewer/message demand, ranking score/breakdown, novelty, and current script product context
- **AND** the full uncontrolled rolling raw-comment window SHALL not be appended as Agent context.
