## ADDED Requirements

### Requirement: Canonical event-ingress diagnostics
Director/runtime diagnostics SHALL expose content-safe counts for events received, accepted, duplicate, safety-rejected, and semantic events scheduled.

#### Scenario: Duplicate retry
- **WHEN** Workbench retries one event ID
- **THEN** diagnostics SHALL increase duplicate count
- **AND** semantic accepted/demand counts SHALL not increase twice.

### Requirement: Stable reducer diagnostics
Diagnostics SHALL expose stable cluster IDs, active cluster count, representative metadata, unique-viewer/message demand, product candidates/confidence, score breakdown, and reconciliation state.

#### Scenario: Reconciliation runs
- **WHEN** reconciliation completes
- **THEN** diagnostics SHALL expose its completion/count/duration metadata
- **AND** the operator SHALL be able to distinguish fast-lane state from reconciliation state.

### Requirement: Agent execution diagnostics
Each reactive Q&A SHALL expose execution-path metadata including ClusterEnvelope identity, factual-fast versus complex-agent path, evidence cache hit/miss, evidence round count, LLM call count, token/latency metrics, and terminal state.

#### Scenario: Tool budget exceeded
- **WHEN** a Q&A terminates because the evidence/tool budget is exhausted
- **THEN** diagnostics SHALL expose the typed budget terminal reason.

### Requirement: Structured-memory diagnostics
Diagnostics SHALL expose bounded sizes/revisions/keys for ScriptState, SessionMemory, TopicMemory, and EvidenceCache without dumping unrestricted private viewer content.

#### Scenario: Long livestream
- **WHEN** the session runs for a long duration
- **THEN** diagnostics SHALL allow operators/tests to verify bounded memory sizes.

### Requirement: Speech-arbiter diagnostics
Diagnostics SHALL expose current script product/version, current sentence index, last completed sentence, next sentence, pending Q&A cluster, arbiter state, and script/Q&A/resume timeline events.

#### Scenario: Q&A between sentences
- **WHEN** Q&A runs after sentence 7 and before sentence 8
- **THEN** diagnostics SHALL record sentence-7 completion, Q&A selection/playback, resume bridge if used, and sentence-8 start.
