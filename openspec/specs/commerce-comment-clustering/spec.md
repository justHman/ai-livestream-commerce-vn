# commerce-comment-clustering Specification

## Purpose

Commerce livestream viewer-comment clustering and actionable demand ranking: shared semantic embedding with one embedding per accepted comment identity/revision, commerce-aware soft routing producing confidence-bearing product candidates, persistent stable demand clusters over the active rolling horizon, actionable unique-viewer-aware ranking, deterministic benchmark gates, bounded Q&A window eligibility, stable-topic cooldown, and two-level cross-product handling with pivot hysteresis on active unique-viewer demand.

## Requirements
### Requirement: Shared semantic embedding service
The Director runtime, fast reducer, reconciliation worker, diagnostics, entity/product retrieval, and coverage matching SHALL use the configured process-level semantic embedding service. Each accepted comment identity/revision SHALL be embedded at most once for normal live reduction, and expired cache entries SHALL be pruned with bounded active state.

#### Scenario: Cluster diagnostics are polled repeatedly
- **WHEN** no new comments arrive between two diagnostic polls
- **THEN** the second poll reuses cached vectors and returns the same active clusters without re-encoding the full buffer

#### Scenario: No new semantic input
- **WHEN** diagnostics are polled repeatedly with no new accepted semantic events
- **THEN** polling SHALL NOT re-encode the active comment population
- **AND** stable cluster diagnostics SHALL be read from the current ClusterStore state.

### Requirement: Explicit semantic readiness
Stage 2 and production modes SHALL require the configured Vietnamese semantic embedder to load successfully. Hashing embeddings SHALL be allowed only in explicit offline or CI modes and SHALL be surfaced as degraded when used interactively.

#### Scenario: Semantic model dependency is unavailable
- **WHEN** the configured semantic model cannot load in Stage 2 mode
- **THEN** readiness reports not-ready with a sanitized embedder error and the UI disables claims of semantic clustering quality

### Requirement: Commerce-aware routing
Accepted comments SHALL receive broad category, commerce-intent, and product-candidate hints before semantic clustering. Supported intents SHALL include price, promotion, stock, size/color, shipping, usage, comparison, buy intent, complaint, social, spam, and off-topic. Product routing SHALL be confidence-bearing candidate evidence, not an irreversible single-product hard partition when evidence is weak. Cluster-level resolution SHALL support zero, one, or multiple products.

#### Scenario: Semantically similar questions target different products
- **WHEN** two price questions refer to different products
- **THEN** they are not merged into the same actionable cluster

#### Scenario: Different wording expresses the same commerce intent
- **WHEN** two comments ask about the same product size using different Vietnamese wording
- **THEN** semantic clustering can merge them within the shared product and intent partition

#### Scenario: Semantically similar questions target different explicit products
- **WHEN** two price questions explicitly identify different products
- **THEN** strong product evidence SHALL prevent an incorrect single-topic merge.

#### Scenario: Comparison references two products
- **WHEN** one question compares P001 and P020
- **THEN** the resulting cluster SHALL be able to retain both product targets.

### Requirement: Actionable cluster ranking
Only actionable demand clusters SHALL enter Q&A ranking. Ranking SHALL distinguish `unique_viewer_count` from `message_count` and include recency, intent/actionability, current-script/product relevance, product-resolution confidence, and novelty. Spam/off-topic/safety-rejected events SHALL not inflate actionable demand.

#### Scenario: Buffer contains greetings and spam
- **WHEN** greetings and promotional spam arrive alongside one stock question
- **THEN** the stock cluster is rankable, greetings are reported separately, and spam is excluded from actionable and unanswered counts

#### Scenario: Repeated one-viewer spam versus broad demand
- **WHEN** one viewer repeats an actionable phrase many times and another cluster is asked by multiple independent viewers
- **THEN** raw repeated message count SHALL not by itself make the first cluster the stronger demand signal.

### Requirement: Deterministic benchmark gate
The repository SHALL contain deterministic Vietnamese commerce reducer fixtures measuring expected same-topic merges, prohibited incompatible merges, stable cluster identity, multi-product comparisons, unique-viewer demand, representative quality, arrival-order sensitivity before/after reconciliation, and bounded-memory behavior. Threshold/weight changes SHALL be supported by benchmark evidence.

#### Scenario: Threshold candidate over-merges products
- **WHEN** a candidate threshold merges questions for different products
- **THEN** the benchmark fails even if the singleton ratio is lower

#### Scenario: Candidate threshold lowers singleton ratio by over-merging
- **WHEN** a candidate merges incompatible product/topic demand
- **THEN** the benchmark SHALL fail even if singleton ratio improves.

### Requirement: Multi-comment Q&A window eligibility
Q&A eligibility SHALL operate over active stable demand clusters from ClusterStore. A Q&A window SHALL open after a product reaches Intro plus Benefit 1 completion; clusters SHALL be ranked by the scorer (product relevance, intent actionability, size, recency, phase, new demand). A singleton MAY remain active for future demand accumulation, but a cluster MUST satisfy the configured unique-viewer/message eligibility policy before entering Q&A.

#### Scenario: Cluster of size 1 appears during Q&A window
- **WHEN** a singleton cluster exists and no new comments on its topic arrive within the same window
- **THEN** it is retained silently and may merge with a future cluster on the same topic when new demand appears

#### Scenario: Singleton later gains independent demand
- **WHEN** a singleton topic receives a semantically compatible comment from another viewer within the active horizon
- **THEN** the same stable cluster MAY become Q&A-eligible without reconstructing the entire historical window.

### Requirement: Configurable Q&A window limits
Each Q&A window SHALL be bounded by either a maximum cluster count (default 2) or a hard timeout in seconds (default 45), whichever fires first. When the eligible queue is exhausted before either limit, the window SHALL close immediately without waiting.

#### Scenario: Window limit reached by count
- **WHEN** two eligible clusters have been answered within a single Q&A window
- **THEN** the window closes and the product advances to the next stage

#### Scenario: Window limit reached by timeout
- **WHEN** 45 seconds elapse and only one cluster has been answered
- **THEN** the window closes and any unanswered eligible clusters are carried to the next window subject to cooldown

### Requirement: Topic cooldown
After a cluster is answered, its topic SHALL enter cooldown (default 120 seconds). During cooldown the same topic SHALL NOT be answered again unless new comments with distinct content arrive. Cooldown identity SHALL be based on stable semantic topic/cluster fingerprint plus relevant entity/product context and novelty, not only `product_id:intent`.

#### Scenario: Recurrent topic within cooldown
- **WHEN** two new comments arrive on a topic that was answered 30 seconds ago
- **THEN** they form a new cluster on that topic but the topic is not re-answered until cooldown expires or the comments present substantively new information

#### Scenario: Same broad intent but different question
- **WHEN** a product receives a new semantically distinct usage question during cooldown for a prior usage topic
- **THEN** stable topic/novelty identity MAY allow the new topic to become eligible
- **AND** it SHALL not be suppressed solely because both have `intent=usage`.

### Requirement: Paraphrased Q&A phrasing
When answering a cluster, the system SHALL paraphrase the shared intent into one clause rather than reading all member comments verbatim. The paraphrase SHALL be grounded in the common question. The answer SHALL be 1–2 concise sentences grounded in product facts.

#### Scenario: Three comments ask about price in different ways
- **WHEN** three members ask "giá bao nhiêu", "nhiêu tiền", and "giá thế nào" about the same product
- **THEN** the spoken answer is "Cả nhà hỏi giá sản phẩm này ạ? Giá chỉ X đồng thôi ạ." not repeating each member verbatim

### Requirement: Configurable answer variant cache
The system SHALL cache answer variants per (product, topic, profile revision, catalog revision) key. Default cache size SHALL be 3 variants per key. The system SHALL select variants in round-robin order when the same topic recurs after cooldown. The cache SHALL be invalidated on profile or catalog revision changes.

#### Scenario: Same price question recurs after cooldown
- **WHEN** the same product price cluster repeats after cooldown and the profile has not changed
- **THEN** the system returns the next cached variant in round-robin order without calling the LLM

#### Scenario: Cache miss triggers generation
- **WHEN** a cluster on a never-before-answered topic wins Q&A ranking
- **THEN** the LLM generates a fresh answer, the result is cached under the current revision key, and all variants are populated

### Requirement: Two-level cross-product handling
Cross-product excursion/pivot decisions SHALL use stable active cluster demand and unique-viewer-aware product demand. When a cluster for product B wins ranking during product A's stage, the system SHALL distinguish two cases:

1. **Q&A excursion**: B ranks well but demand does not cross the pivot threshold. The system SHALL answer one Q&A for B and return to product A's exact checkpoint.
2. **Demand pivot**: B's demand share reaches the enter threshold (default >= 60% of recent actionable comments, minimum 5 unique comments, score margin >= 0.15). The system SHALL checkpoint product A, invalidate prepared B turns, run the full B lifecycle (opening skipped, Intro→Benefit→Offer→Trust→CTA→Transition), and resume A immediately when B demand exits (share < 45%). If B demand remains hot after the full lifecycle, a new B lifecycle runs with fresh variants and cache.

Multi-product comparison clusters SHALL remain Q&A topics and SHALL not be misinterpreted automatically as a full demand pivot.

#### Scenario: Comparison cluster
- **WHEN** viewers compare current product A with B
- **THEN** the runtime MAY answer the comparison without interpreting every comparison viewer as exclusive pivot demand for B.

#### Scenario: Quick price question for product B during A's Benefit stage
- **WHEN** a single cluster for product B wins the Q&A ranking with moderate demand share
- **THEN** one Q&A turn for B is spoken, the checkpoint is restored, and product A's Benefit resumes

#### Scenario: Strong demand shift to product B
- **WHEN** 8 of 12 recent actionable comments target product B continuously
- **THEN** a demand pivot fires, product A is checkpointed, B runs a full lifecycle (without global opening), and A resumes when B demand exits below 45%

### Requirement: Pivot hysteresis
Demand pivot enter/exit thresholds SHALL operate on active unique-viewer-aware resolved demand with configured confidence and score-margin gates. The demand pivot SHALL enter when demand share >= 60%. It SHALL exit when demand share drops below 45%. The system MUST NOT re-pivot within a product that was just exited (nested pivot is forbidden). Demand for a third product C SHALL be queued.

#### Scenario: One viewer floods B comments
- **WHEN** one viewer sends enough repeated B comments to exceed the historical raw-message threshold
- **THEN** the runtime SHALL not enter a demand pivot unless the configured unique-viewer-aware criteria also pass.

#### Scenario: Demand oscillates between 55% and 50%
- **WHEN** product B demand fluctuates between 50% and 55%
- **THEN** neither enter (below 60%) nor exit (above 45%) fires, so the current product continues without oscillation

### Requirement: No nested pivots
A demand pivot SHALL complete its full lifecycle before a second pivot is considered. Demand for a third product C SHALL remain queued during an active B lifecycle and be evaluated after the pivot ends or A resumes, whichever comes later.

#### Scenario: Product C demand rises during B pivot
- **WHEN** product C comments surge while B's pivot lifecycle is active
- **THEN** C demand is recorded and queued but not evaluated until B's lifecycle ends and A either resumes or C wins the next evaluation

