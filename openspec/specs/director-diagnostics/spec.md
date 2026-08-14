# director-diagnostics Specification

## Purpose
TBD - created by archiving change stage2-console-redesign. Update Purpose after archive.
## Requirements
### Requirement: Canonical queue and speech counters
The system SHALL expose per-session counters named `received_total`, `buffered_comments`, `director_cycles`, `queued_decisions`, and `completed_speeches`. `received_total` SHALL count accepted comments over the session lifetime; `buffered_comments` SHALL count comments retained in the bounded rolling buffer; `queued_decisions` SHALL count decisions actually waiting to speak; and `completed_speeches` SHALL increment only after playback completion is confirmed.

#### Scenario: Comments accumulate while speech is active
- **WHEN** 89 comments have been accepted and one speech is still active
- **THEN** diagnostics report `received_total=89`, the truthful rolling-buffer count, the active decision, zero or more actual queued decisions, and no increment for the unfinished speech

### Requirement: Truthful speech-turn diagnostics
The system SHALL expose the active decision, selected cluster members, generation prompt or verbatim input, generated script, playback state, upcoming decisions, and bounded completed-speech history as distinct fields. A generated script SHALL never be labeled as current before generation completes.

#### Scenario: Generated product introduction completes
- **WHEN** an `introduce_product` turn finishes LLM generation, TTS, and avatar playback
- **THEN** the completed history contains its input prompt and generated script while the active field moves to the next turn or becomes empty

### Requirement: Director-equivalent diagnostic snapshot
Cluster diagnostics SHALL use the active session's embedder instance, cached embeddings, `selection_window_sec`, and `cluster_merge_threshold`. Diagnostic code MUST NOT read private queue storage directly or hardcode an independent threshold.

#### Scenario: Comments age out of the selection window
- **WHEN** a buffered comment is older than the Director's configured selection window
- **THEN** it remains eligible for buffer accounting but is excluded from `active_comments` and active cluster metrics

### Requirement: Embedder and cluster quality status
Diagnostics SHALL expose `embedder_name`, `embedder_status`, effective threshold, active comment count, total clusters, multi-comment clusters, singleton clusters, actionable clusters, and unanswered clusters.

#### Scenario: Lexical fallback is active
- **WHEN** semantic embedder initialization fails outside an explicit offline-test mode
- **THEN** diagnostics identify the hashing embedder and report a degraded status rather than claiming semantic clustering is ready

### Requirement: Ranked cluster score breakdown
When a cluster is selected for speech, diagnostics SHALL display the cluster ID, topic, product, intent, member count, full member list, total score, and per-factor score breakdown (product relevance, intent/actionability, size, recency, phase bonus, new demand bonus). Every score factor SHALL be labeled individually.

#### Scenario: Operator inspects a selected cluster
- **WHEN** the Q&A window selects a cluster for speech
- **THEN** the diagnostics panel shows the complete score breakdown with each factor's contribution

### Requirement: Prompt layer diagnostics
Each completed or active speech turn SHALL carry a `prompt_layers` field containing the composite `base_role`, `shop_profile`, `stage_task`, and the `final_prompt` as submitted to the LLM. Each layer SHALL be independently accessible and fully visible.

#### Scenario: Operator reviews what the LLM received
- **WHEN** inspecting a completed turn
- **THEN** the diagnostics show all four prompt layers separately without truncation and the final prompt is copyable

### Requirement: Queue lifecycle and revision state
Diagnostics SHALL expose each turn's lifecycle position (queued, preparing, prepared, playback, completed, failed, cancelled_stale) and the current `profile_revision`, `catalog_revision`, `config_revision`, and `generation_token`. Stale turns SHALL be identified with their cancellation reason.

#### Scenario: Turn is invalidated by config change
- **WHEN** a prepared turn's `generation_token` is older than the current `config_revision`
- **THEN** diagnostics show that turn as `cancelled_stale` with the stale revision information

### Requirement: Module-boundary latency spans
The diagnostics snapshot SHALL expose per-turn latency for each pipeline stage: ingest→queue→embed→route→cluster→rank→decision wait→LLM TTFT/total→TTS first audio/total/RTF→avatar speak_started→speak_ended. Overlapping spans (e.g., preparation during playback) SHALL NOT be double-counted in wall-clock.

#### Scenario: Turn pipeline is profiled
- **WHEN** a turn completes end-to-end successfully
- **THEN** diagnostics include the latency for each pipeline stage with start and end timestamps

### Requirement: Compatibility migration
During the migration window, legacy fields MAY remain as aliases, but canonical fields SHALL be authoritative and the Stage 2 console SHALL consume only canonical fields.

#### Scenario: Legacy client requests diagnostics
- **WHEN** a response includes both `pending` and `buffered_comments`
- **THEN** both values represent the same buffer count and new clients use `buffered_comments`


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
