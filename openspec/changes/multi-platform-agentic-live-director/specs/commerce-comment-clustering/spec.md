## MODIFIED Requirements

### Requirement: Shared semantic embedding service
The Director runtime, fast reducer, reconciliation worker, diagnostics, entity/product retrieval, and coverage matching SHALL use the configured process-level semantic embedding service. Each accepted comment identity/revision SHALL be embedded at most once for normal live reduction, and expired cache entries SHALL be pruned with bounded active state.

#### Scenario: No new semantic input
- **WHEN** diagnostics are polled repeatedly with no new accepted semantic events
- **THEN** polling SHALL NOT re-encode the active comment population
- **AND** stable cluster diagnostics SHALL be read from the current ClusterStore state.

### Requirement: Commerce-aware routing
Accepted comments SHALL receive broad category, commerce-intent, and product-candidate hints before semantic clustering. Product routing SHALL be confidence-bearing candidate evidence, not an irreversible single-product hard partition when evidence is weak. Cluster-level resolution SHALL support zero, one, or multiple products.

#### Scenario: Semantically similar questions target different explicit products
- **WHEN** two price questions explicitly identify different products
- **THEN** strong product evidence SHALL prevent an incorrect single-topic merge.

#### Scenario: Comparison references two products
- **WHEN** one question compares P001 and P020
- **THEN** the resulting cluster SHALL be able to retain both product targets.

### Requirement: Actionable cluster ranking
Only actionable demand clusters SHALL enter Q&A ranking. Ranking SHALL distinguish `unique_viewer_count` from `message_count` and include recency, intent/actionability, current-script/product relevance, product-resolution confidence, and novelty. Spam/off-topic/safety-rejected events SHALL not inflate actionable demand.

#### Scenario: Repeated one-viewer spam versus broad demand
- **WHEN** one viewer repeats an actionable phrase many times and another cluster is asked by multiple independent viewers
- **THEN** raw repeated message count SHALL not by itself make the first cluster the stronger demand signal.

### Requirement: Deterministic benchmark gate
The repository SHALL contain deterministic Vietnamese commerce reducer fixtures measuring expected same-topic merges, prohibited incompatible merges, stable cluster identity, multi-product comparisons, unique-viewer demand, representative quality, arrival-order sensitivity before/after reconciliation, and bounded-memory behavior. Threshold/weight changes SHALL be supported by benchmark evidence.

#### Scenario: Candidate threshold lowers singleton ratio by over-merging
- **WHEN** a candidate merges incompatible product/topic demand
- **THEN** the benchmark SHALL fail even if singleton ratio improves.

### Requirement: Multi-comment Q&A window eligibility
Q&A eligibility SHALL operate over active stable demand clusters from ClusterStore. A singleton MAY remain active for future demand accumulation, but a cluster MUST satisfy the configured unique-viewer/message eligibility policy before entering Q&A.

#### Scenario: Singleton later gains independent demand
- **WHEN** a singleton topic receives a semantically compatible comment from another viewer within the active horizon
- **THEN** the same stable cluster MAY become Q&A-eligible without reconstructing the entire historical window.

### Requirement: Topic cooldown
Cooldown identity SHALL be based on stable semantic topic/cluster fingerprint plus relevant entity/product context and novelty, not only `product_id:intent`.

#### Scenario: Same broad intent but different question
- **WHEN** a product receives a new semantically distinct usage question during cooldown for a prior usage topic
- **THEN** stable topic/novelty identity MAY allow the new topic to become eligible
- **AND** it SHALL not be suppressed solely because both have `intent=usage`.

### Requirement: Two-level cross-product handling
Cross-product excursion/pivot decisions SHALL use stable active cluster demand and unique-viewer-aware product demand. Multi-product comparison clusters SHALL remain Q&A topics and SHALL not be misinterpreted automatically as a full demand pivot.

#### Scenario: Comparison cluster
- **WHEN** viewers compare current product A with B
- **THEN** the runtime MAY answer the comparison without interpreting every comparison viewer as exclusive pivot demand for B.

### Requirement: Pivot hysteresis
Demand pivot enter/exit thresholds SHALL operate on active unique-viewer-aware resolved demand with configured confidence and score-margin gates.

#### Scenario: One viewer floods B comments
- **WHEN** one viewer sends enough repeated B comments to exceed the historical raw-message threshold
- **THEN** the runtime SHALL not enter a demand pivot unless the configured unique-viewer-aware criteria also pass.
