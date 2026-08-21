# Approved Script Authoring Pipeline Specification

# Approved Script Authoring Pipeline Specification

## Purpose

Pre-live multi-product script authoring for VN AI livestream: deterministic ScriptGate validation, optional bounded AI generation/repair, immutable versioning, human-only approval, REST+SSE workflow APIs, workbench UX, and runtime binding of approved spoken_text through the canonical Change A source-agnostic TextChunker path. Gate-first and AI-optional; the backend owns the finite workflow and no general agent loop exists.

## Requirements

### Requirement: Change A final-architecture and benchmark readiness gate
The system SHALL keep `approved-script-authoring-pipeline` blocked until `adaptive-speech-text-chunking` has completed its mandatory final-architecture correction, required verification/strict OpenSpec validation, and VieNeu benchmark PASS evidence.

#### Scenario: Change A benchmark has not passed
- **GIVEN** Change A is missing VieNeu PASS evidence or is marked NOT PASS
- **WHEN** implementation/readiness of Change B is evaluated
- **THEN** Change B SHALL remain blocked
- **AND** no production-ready claim for approved-script runtime integration SHALL be made.

#### Scenario: Change A still has transitional architecture
- **GIVEN** any active Change A state still contains a parallel `backend.application.speech_chunking` implementation, sibling `backend/application/text_chunker.py` facade, duplicate/re-exported `render.windows.TextChunk`, full-script bypass, chunker-owned streaming timeout, contaminated adaptive fixed-character config, duplicated chunking defaults, or unresolved finality architecture
- **WHEN** Change B readiness is evaluated
- **THEN** Change B SHALL remain blocked even if tests happen to pass through a compatibility path
- **AND** Change B SHALL NOT add a shim or duplicate implementation to compensate.

#### Scenario: Change A is ready
- **GIVEN** Change A uses the final cohesive `backend.application.text_chunker` package, exactly one canonical TextChunk, the same TextChunker for complete and incremental text, corrected timing/policy/config/finality ownership, green strict validation, and the required VieNeu benchmark PASS
- **WHEN** Change B implementation begins
- **THEN** the final Change A package contract SHALL be treated as the downstream speech-segmentation dependency.

### Requirement: Pre-live ScriptSet aggregate
The system SHALL represent scripts for a planned multi-product livestream as a `ScriptSet` independent of runtime session creation.

#### Scenario: Create authoring set before live session
- **GIVEN** a shop/product catalog and planned livestream brief
- **WHEN** a user creates a ScriptSet
- **THEN** authoring SHALL be possible without creating an avatar/TTS/live session
- **AND** the ScriptSet SHALL record the ordered/selected products and LiveSessionBrief.

#### Scenario: Runtime session binds later
- **GIVEN** a ScriptSet with required fresh approvals
- **WHEN** a runtime session is prepared
- **THEN** the ScriptSet MAY be bound to that session through an explicit binding command.

### Requirement: Gate-first AI-optional workflow
The system SHALL run deterministic ScriptGate validation before requiring AI assistance and SHALL not call an LLM for a compliant manual draft.

#### Scenario: Manual draft passes directly
- **GIVEN** a user-written draft
- **WHEN** the user submits it and ScriptGate passes
- **THEN** the version SHALL become `REVIEWABLE`
- **AND** zero LLM generation/repair calls SHALL be required for that transition.

#### Scenario: Manual draft fails
- **GIVEN** a user-written draft
- **WHEN** ScriptGate returns violations
- **THEN** the version SHALL become `GATE_FAILED`
- **AND** the user SHALL be able to edit manually or explicitly request AI Fix.

### Requirement: Human-only approval
The system SHALL require an authenticated human approval action after gate PASS; automated generation, repair, or gate PASS SHALL never approve content.

#### Scenario: Gate pass is not approval
- **GIVEN** a version whose latest Full Script Gate passed
- **WHEN** no human has approved it
- **THEN** its state SHALL remain `REVIEWABLE`
- **AND** it SHALL not be runtime-eligible as approved content.

#### Scenario: Human approves current version
- **GIVEN** a current fresh `REVIEWABLE` version
- **WHEN** an authorized human approves that exact version
- **THEN** an immutable approval record SHALL be created
- **AND** that exact version MAY become runtime-eligible.

### Requirement: Versioned shared rule registry
The system SHALL maintain one canonical versioned `ScriptRuleRegistry` that drives deterministic gate checks and supplies prompt constraints/instructions without duplicating rule truth.

#### Scenario: Generation uses registry constraints
- **GIVEN** active generation-relevant rules
- **WHEN** a generation prompt is built
- **THEN** relevant `generation_constraint` values SHALL come from the canonical registry/version.

#### Scenario: Repair uses only failed rules
- **GIVEN** a failed gate run with rule IDs
- **WHEN** an AI repair prompt is built
- **THEN** only those relevant rules' `repair_instruction` values plus required authoritative facts SHALL be injected
- **AND** unrelated rule text SHALL not be included merely by default.

### Requirement: Deterministic ScriptGate authority
The system SHALL use deterministic ScriptGate evaluation as the authority for policy pass/fail; LLM self-assessment SHALL not replace it.

#### Scenario: AI claims a violation is fixed
- **GIVEN** an AI Fix result
- **WHEN** the model states or implies that the draft is compliant
- **THEN** the result SHALL still be a new `DRAFT`
- **AND** ScriptGate SHALL run again before it may become `REVIEWABLE`.

#### Scenario: Gate completes with content violations
- **WHEN** a syntactically valid submit request produces deterministic policy violations
- **THEN** the API SHALL return a successful gate-execution response with domain state `gate_failed`
- **AND** SHALL not represent the violation as a transport/schema failure.

### Requirement: Display and spoken representations
The system SHALL preserve user-facing `display_text` and exact TTS-facing `spoken_text` representations whenever normalization changes how content will be spoken. A fresh human-approved version SHALL remain the exact immutable source of runtime `spoken_text`. Live runtime MAY derive deterministic sentence-span/cursor metadata from that exact text for scheduling, but SHALL NOT rewrite, paraphrase, mutate, or create a new approved artifact.

#### Scenario: Runtime sentence map
- **GIVEN** a fresh approved product script
- **WHEN** it is bound to a live session
- **THEN** runtime MAY derive sentence offsets/text slices
- **AND** concatenating those slices SHALL reproduce the exact approved `spoken_text`.

#### Scenario: Price normalization
- **GIVEN** display text containing a compact price/percent form
- **WHEN** deterministic TTS-readiness normalization applies
- **THEN** the produced `spoken_text` SHALL represent the intended spoken form
- **AND** the human review UI SHALL show that exact spoken representation before approval.

#### Scenario: Approved spoken artifact reaches runtime
- **GIVEN** an approved fresh version
- **WHEN** it is used in a live session
- **THEN** the canonical Change A speech path SHALL receive the exact approved `spoken_text`
- **AND** no post-approval LLM rewrite or source-specific chunking transformation SHALL occur before TextChunker ingestion.

### Requirement: Canonical Change A package integration
The system SHALL integrate approved-script runtime speech only through Change A's final canonical `backend.application.text_chunker` package contract and SHALL NOT depend on transitional or duplicate chunking namespaces/types.

#### Scenario: Runtime imports Change A types
- **WHEN** Change B production/runtime integration needs `TextChunker` or `TextChunk`
- **THEN** it SHALL import them from `backend.application.text_chunker`
- **AND** SHALL NOT import `backend.application.speech_chunking` or `TextChunk` from `backend.application.render.windows`.

#### Scenario: Missing upstream public interface
- **GIVEN** Change B requires a Change A capability that is not available through the stable package contract
- **WHEN** implementation reaches that dependency
- **THEN** the upstream Change A contract SHALL be corrected first
- **AND** Change B SHALL NOT deep-import an internal module or duplicate the capability as a workaround.

### Requirement: Complete approved scripts use the same source-agnostic TextChunker
Approved runtime speech SHALL continue to use the canonical Change A `TextChunker` through the existing verbatim speech service. The new sentence scheduler sits above this path and invokes it per exact approved sentence or other deterministic approved span; it SHALL NOT create a script-specific chunker.

#### Scenario: Approved sentence is spoken
- **WHEN** the runtime schedules one approved sentence
- **THEN** that exact sentence SHALL enter the canonical verbatim TextChunker/TTS/render path
- **AND** TextChunker MAY split it into phrase-sized chunks.

#### Scenario: Full approved script is spoken
- **GIVEN** an approved fresh product script whose entire `spoken_text` is already available
- **WHEN** runtime requests speech for that script
- **THEN** the complete text SHALL enter the canonical Change A TextChunker path and be segmented before TTS
- **AND** the runtime SHALL NOT construct one giant `TextChunk` directly around the whole script.

#### Scenario: Fixed rollback is active
- **GIVEN** Change A runtime is explicitly rolled back from `adaptive_vi` to the fixed strategy
- **WHEN** the same approved script is spoken
- **THEN** Change B SHALL still use the same TextChunker capability and runtime speech path
- **AND** SHALL NOT select a separate script chunker implementation.

### Requirement: Change B does not own streaming timing or chunk-policy baggage
The system SHALL keep realtime deadline scheduling and TextChunker policy configuration under Change A/runtime orchestration ownership and SHALL NOT introduce source-specific chunker configuration in Change B.

#### Scenario: Complete script binding
- **GIVEN** a complete approved script with no upstream token wait
- **WHEN** Change B binds it to runtime
- **THEN** Change B SHALL NOT configure `flush_timeout_ms`, call `check_timeout`, or start an LLM-stream deadline timer for that script.

#### Scenario: Adaptive runtime policy
- **WHEN** Change A uses `adaptive_vi`
- **THEN** Change B SHALL NOT supply fixed-policy `target_chars` as an adaptive authoring concept
- **AND** SHALL NOT pass a source mode such as `script` or `llm` into TextChunker.

### Requirement: Canonical speech-duration estimation is reused
The system SHALL use Change A's canonical deterministic speech-duration estimation interface for actual generated/manual `spoken_text` duration checks and SHALL NOT implement a second Vietnamese speech-duration estimator in Change B.

#### Scenario: Segment gate checks duration
- **GIVEN** a generated or manually edited segment with `spoken_text`
- **WHEN** ScriptGate evaluates target spoken duration
- **THEN** duration SHALL be estimated through the stable Change A duration-estimation contract.

#### Scenario: Generation preview before text exists
- **GIVEN** no generated prose exists yet
- **WHEN** Change B previews `K` and semantic-call budget
- **THEN** it MAY use a separate model-output `GenerationBudgetCalibration` based on provider output limits/statistics
- **AND** that calibration SHALL NOT duplicate or masquerade as Change A's speech-duration estimator.

### Requirement: TextChunk finality remains Change A-owned
Sentence-level script cursor semantics SHALL NOT stamp or reinterpret TextChunk finality. Runtime SHALL determine sentence completion at the speech-call/scheduler level while Change A remains responsible for exactly-once TextChunk→AudioWindow→VideoWindow finality. Change B SHALL supply approved text but SHALL NOT manually construct, rewrite, or stamp `TextChunk.is_final`.

#### Scenario: Sentence has multiple TextChunks
- **WHEN** one approved sentence produces several TextChunks
- **THEN** intermediate/final TextChunk flags SHALL remain Change A concerns
- **AND** script cursor advancement SHALL not be implemented by treating each TextChunk as a sentence.

#### Scenario: Approved script reaches EOF with no remainder to restamp
- **WHEN** the complete approved text has already been consumed by the canonical Change A speech path and normal completion occurs
- **THEN** Change B SHALL not reconstruct an already-emitted TextChunk merely to set `is_final`
- **AND** Change A's finalization protocol SHALL express normal completion exactly once without duplicating text/audio/video content.

#### Scenario: Runtime error or cancellation
- **WHEN** approved-script playback is cancelled or fails
- **THEN** Change B SHALL not fabricate a normal final TextChunk marker
- **AND** runtime SHALL follow Change A's error/cancel finality semantics.

### Requirement: Project-owned generation skill
The system SHALL use a repository-owned `livestream-sales-script` skill for creative generation and SHALL not fetch mutable third-party skill content during runtime requests.

#### Scenario: Runtime generation loads skill
- **WHEN** a Generate operation builds its system instruction
- **THEN** it SHALL load the project-owned skill version/hash packaged with the backend
- **AND** record that version/hash in the GenerationFingerprint.

#### Scenario: Repair operation
- **WHEN** a Fix with AI operation runs
- **THEN** it SHALL NOT load the sales-generation skill
- **AND** SHALL use constrained failed-rule repair instructions instead.

### Requirement: Generate and Fix contracts differ
The system SHALL keep creative generation and constrained repair as separate prompt contracts.

#### Scenario: Generate script
- **WHEN** the user explicitly requests generation
- **THEN** the model input SHALL include sales skill guidance, generation rules, authoritative context, intent/duration, and plan/segment assignment as applicable.

#### Scenario: Fix failed draft
- **WHEN** the user explicitly requests AI Fix for a gate-failed version
- **THEN** the model SHALL be instructed to make the minimum changes needed for supplied violations
- **AND** preserve compliant meaning, structure, tone, and facts unless a failed rule requires change.

### Requirement: No general agentic control loop
The system SHALL keep workflow control in backend code and SHALL not allow the LLM to control product traversal, tool invocation, job creation, retry count, or segment count.

#### Scenario: Model requests another tool/call
- **GIVEN** a generation result that suggests calling another tool or generating another segment
- **WHEN** backend workflow state is evaluated
- **THEN** the suggestion SHALL NOT create work beyond the precomputed finite workflow.

#### Scenario: No arbitrary tools
- **WHEN** planner/segment/repair LLM calls are made
- **THEN** the runtime SHALL not expose arbitrary filesystem, web, job-management, or product-traversal tools to the model.

### Requirement: Deterministic generation preview
The system SHALL provide a no-LLM preview of planned segment counts and semantic call budget before a user starts long-form or batch generation.

#### Scenario: Preview one 60-minute product
- **GIVEN** model-capability/calibration configuration and target duration 3600 seconds
- **WHEN** the user requests generation preview
- **THEN** the backend SHALL return the planned segment count `K` and estimated semantic calls `1 + K` using Change B `GenerationBudgetCalibration`
- **AND** no LLM call SHALL be made for the preview
- **AND** the preview SHALL NOT require or duplicate Change A speech-duration estimation for text that does not yet exist.

#### Scenario: Preview batch
- **GIVEN** multiple selected products with target durations
- **WHEN** the user requests a batch preview
- **THEN** the response SHALL include per-product and total planned semantic-call counts.

### Requirement: Long-form product planning
The system SHALL generate long-form product scripts through one bounded structured `ProductScriptPlan` call before segment prose generation.

#### Scenario: Plan 10–60 minute script
- **GIVEN** authoritative product context and target spoken duration
- **WHEN** generation begins
- **THEN** exactly one normal semantic planning call SHALL produce a schema-validated content plan
- **AND** the plan SHALL distribute topics/facts/objections/CTA intent across bounded segments.

#### Scenario: Plan references unknown fact
- **GIVEN** planner output referencing a fact/objection ID not supplied by the backend
- **WHEN** plan schema/content validation runs
- **THEN** the invalid reference SHALL be rejected
- **AND** SHALL not silently become an authoritative claim.

### Requirement: Fixed segment-count bound
The backend SHALL compute and persist a finite segment count `K` before segment generation and SHALL not allow model output to increase it.

#### Scenario: Segment count computed
- **GIVEN** target duration and safe model output calibration
- **WHEN** the product plan is accepted
- **THEN** the backend SHALL compute/persist `K`
- **AND** valid normal segment indices SHALL be limited to `0..K-1`.

#### Scenario: Model asks to continue after final segment
- **GIVEN** segment `K-1` completed
- **WHEN** model prose/metadata suggests more content
- **THEN** no segment `K` SHALL be automatically created.

### Requirement: Sequential segment continuity
The system SHALL generate segments for one product sequentially using compact validated continuity state instead of repeatedly injecting the full prior script or adding a separate summary-model call.

#### Scenario: Generate next segment
- **GIVEN** segment N passed its segment gate
- **WHEN** segment N+1 is generated
- **THEN** its prompt SHALL receive validated continuity metadata including relevant covered facts/objections/CTA/topic state
- **AND** SHALL not require an additional LLM summary call between segments.

#### Scenario: Invalid continuity ID
- **GIVEN** model continuity metadata references an unknown authoritative ID
- **WHEN** the backend validates it
- **THEN** the invalid reference SHALL be rejected/ignored according to deterministic schema policy
- **AND** SHALL not expand authoritative context.

### Requirement: Segment gate stops future spend
The system SHALL run Segment Gate after each generated segment and SHALL stop scheduling later segments for that product when the segment fails content policy.

#### Scenario: Segment N fails
- **GIVEN** a plan with K segments and segment N fails ScriptGate
- **WHEN** the product workflow processes the failure
- **THEN** the failing segment SHALL be regenerated IN PLACE up to the configured backend-owned `segment_max_attempts` bound (a fixed constant, not model-controlled), keeping prior passing segments and continuity
- **AND** segments N+1 through K-1 SHALL not be semantically generated until segment N passes its gate or the in-place attempts are exhausted.

#### Scenario: In-place retry exhausted without a pass
- **GIVEN** segment N has been regenerated `segment_max_attempts` times
- **WHEN** every attempt still fails Segment Gate
- **THEN** segment N SHALL become gate-failed
- **AND** segments N+1 through K-1 SHALL not be semantically generated until human action resolves the failure.

#### Scenario: No unbounded automatic repair loop
- **GIVEN** a gate-failed segment (after the fixed in-place retry bound is exhausted)
- **WHEN** no human explicitly requests repair/regeneration
- **THEN** the system SHALL make no additional semantic repair/regeneration call for that failure.
- **AND** the in-place retry SHALL be bounded by the backend-owned `segment_max_attempts` constant (never a model-controlled or unbounded loop).

### Requirement: Full-script gate
The system SHALL compile selected passing segment versions and run a Full Script Gate before a product script becomes reviewable.

#### Scenario: All segments pass locally
- **GIVEN** all K selected segments passed Segment Gate
- **WHEN** the product is compiled
- **THEN** Full Script Gate SHALL check cross-segment repetition, coverage, contradictions, CTA pacing, tone, transitions, and total duration.

#### Scenario: Full gate fails
- **WHEN** cross-segment validation finds a violation
- **THEN** the compiled product SHALL not become `REVIEWABLE`
- **AND** the response/UI SHALL identify actionable global or implicated-segment violations.

### Requirement: Multi-product bounded batch generation
The system SHALL support one-click multi-product generation as backend-managed per-product workflows with bounded product concurrency and per-product isolation.

#### Scenario: Generate all selected products
- **GIVEN** N selected/missing products
- **WHEN** the user starts Generate All
- **THEN** the backend SHALL create one bounded product workflow per selected product
- **AND** SHALL not request one giant LLM response containing all product scripts.

#### Scenario: One product fails
- **GIVEN** sibling products in the same batch
- **WHEN** one product gate/provider workflow fails
- **THEN** completed/passing sibling product artifacts SHALL remain valid
- **AND** the batch SHALL expose per-product state rather than roll back all siblings.

### Requirement: Product-level concurrency bound
The system SHALL enforce configured backend concurrency across active product workflows while keeping segments within a product sequential by default.

#### Scenario: More products than concurrency
- **GIVEN** 20 products and configured concurrency 3
- **WHEN** the batch runs
- **THEN** no more than 3 product workflows SHALL perform semantic generation concurrently
- **AND** remaining products SHALL stay queued until capacity becomes available.

### Requirement: Bounded retries
The system SHALL distinguish finite provider/transport retries from semantic content retries.

#### Scenario: Transient provider failure
- **WHEN** a configured retryable provider error occurs
- **THEN** the same immutable job input MAY be retried up to configured `max_attempts`
- **AND** the finite attempt count SHALL be persisted/observable.

#### Scenario: Content gate failure
- **WHEN** model output is syntactically returned but fails content gate
- **THEN** the system SHALL not auto-regenerate or auto-repair it.

### Requirement: Generation idempotency
The system SHALL protect asynchronous generation commands from duplicate user/browser submission that would otherwise double-spend model calls.

#### Scenario: Duplicate Generate All request
- **GIVEN** an equivalent batch is already queued/running under the same idempotency identity
- **WHEN** the request is repeated
- **THEN** the API SHALL return/refer to the existing workflow
- **AND** SHALL not create duplicate semantic jobs.

### Requirement: Immutable script versioning
The system SHALL create immutable plan, segment, and compiled script versions for manual edits and AI operations rather than mutating approved/history artifacts in place.

#### Scenario: User edits approved script
- **GIVEN** approved version v4
- **WHEN** the user edits content
- **THEN** a new draft version SHALL be created
- **AND** v4 SHALL remain immutable history
- **AND** the new version SHALL require gating/review/approval.

#### Scenario: Regenerate one segment
- **WHEN** a user explicitly regenerates one segment
- **THEN** only a new version of that segment SHALL be created
- **AND** unaffected sibling segment versions SHALL remain reusable.

### Requirement: Reproducible generation fingerprint
The system SHALL record model, skill, rule, prompt-template, authoritative-context, generation-parameter, and plan/version fingerprints sufficient to explain how an AI draft was produced without storing chain-of-thought.

#### Scenario: Skill changes
- **GIVEN** a later generation uses a different project skill version/hash
- **WHEN** versions are compared
- **THEN** their GenerationFingerprints SHALL expose that difference.

### Requirement: Dependency-bound approval
Approval SHALL be bound to exact spoken content and configured authoritative dependency versions and SHALL become stale when those dependencies change.

#### Scenario: Promotion changes after approval
- **GIVEN** an approved script mentioning a promotion
- **WHEN** the authoritative promotion version changes
- **THEN** the approval SHALL become stale/not runtime-eligible until revalidated and reapproved.

#### Scenario: Script text changes
- **WHEN** approved spoken content changes by creating a new version
- **THEN** the prior approval SHALL not transfer automatically to the new version.

### Requirement: Transition policy
The system SHALL support `ORDER_AWARE` and `ORDER_AGNOSTIC` product transition policies without requiring different generator implementations.

#### Scenario: Dynamic product order
- **GIVEN** `ORDER_AGNOSTIC`
- **WHEN** product scripts are generated
- **THEN** the core spoken script SHALL not hard-code a specific previous/next product dependency that prevents runtime reordering.

#### Scenario: Locked product order
- **GIVEN** `ORDER_AWARE`
- **WHEN** generation context includes adjacent product summaries
- **THEN** the script MAY include explicit compatible transitions.

### Requirement: REST authoring API
The system SHALL expose authoring resources and commands under `/api/v1/script-sets` using REST/JSON and existing backend authentication/authorization conventions.

#### Scenario: Async generation accepted
- **WHEN** a valid Generate/Fix/Regenerate/Generate All command is accepted
- **THEN** the API SHALL return `202 Accepted` with stable workflow/job identifiers.

#### Scenario: Invalid state transition
- **WHEN** a user requests AI Fix for a version that is not eligible for fix
- **THEN** the API SHALL return `409 Conflict` with a stable domain error code.

### Requirement: SSE generation progress
The system SHALL expose one-way generation progress through Server-Sent Events rather than introducing a new authoring WebSocket/agent protocol.

#### Scenario: Batch progress stream
- **GIVEN** an active generation batch
- **WHEN** an authorized client subscribes to its events endpoint
- **THEN** it SHALL receive ordered, deduplicable lifecycle events carrying stable script/batch/product/segment IDs.

#### Scenario: Client reconnects
- **WHEN** the SSE connection reconnects after interruption
- **THEN** the client SHALL be able to recover current batch snapshot and continue without creating new generation jobs.

### Requirement: Session binding validates readiness
The system SHALL bind a ScriptSet to a runtime session only when required script artifacts are approved and fresh.

#### Scenario: Missing or stale product script
- **WHEN** binding detects a required missing/unapproved/stale product
- **THEN** the API SHALL reject binding with `409 Conflict`
- **AND** include structured `missing`/`stale` product details.

#### Scenario: Valid binding
- **GIVEN** all required artifacts are fresh/approved
- **WHEN** binding succeeds
- **THEN** runtime selection SHALL resolve exact approved `spoken_text`
- **AND** hand the complete text to the canonical Change A source-agnostic TextChunker path in-process
- **AND** SHALL not construct a giant TextChunk or use a parallel full-script segmentation path.

### Requirement: Curated Vietnamese safety resources
The system SHALL treat external profanity/toxicity datasets as curated policy inputs/evaluation sources rather than direct production blacklists.

#### Scenario: Add dataset-derived lexicon
- **WHEN** a dataset-derived lexicon/pattern resource is introduced
- **THEN** source/license/version provenance SHALL be recorded
- **AND** human curation and false-positive tests SHALL exist before activation.

#### Scenario: Brand/product allowlist
- **GIVEN** a token that resembles a blocked variant but is an authorized brand/product term
- **WHEN** the relevant rule evaluates it
- **THEN** deterministic allowlist/context policy SHALL prevent the known false positive.

### Requirement: Workflow persistence and recovery
The system SHALL persist enough finite workflow state to recover generation after process restart without inferring next actions from model prose.

#### Scenario: Worker restarts mid-product
- **GIVEN** persisted plan K and completed segment indices
- **WHEN** the worker restarts
- **THEN** recovery SHALL resume from the next persisted finite step
- **AND** SHALL not regenerate completed immutable segments unless an explicit retry policy requires it.

### Requirement: Cancellation stops future semantic calls
The system SHALL allow cancellation of a generation batch/product workflow and stop scheduling new semantic calls while preserving completed immutable artifacts.

#### Scenario: Cancel batch
- **WHEN** an authorized user cancels an active batch
- **THEN** pending work SHALL be marked cancelled/not scheduled
- **AND** completed versions SHALL remain available
- **AND** a terminal cancellation event SHALL be emitted.

### Requirement: Content-private observability
The system SHALL expose call/gate/workflow/cost diagnostics without logging raw script text by default.

#### Scenario: Generation telemetry
- **WHEN** generation runs
- **THEN** telemetry SHALL include target duration, planned K, semantic call counts, provider attempts, latency/token metadata when available, and gate rule IDs
- **AND** SHALL omit raw prompt/script text from normal logs.

### Requirement: Runtime Q&A does not mutate authoring state
Reactive Agentic Director answers, lead-ins, resume bridges, script cursor checkpoints, and demand state SHALL be runtime-only artifacts and SHALL NOT mutate ScriptSet versions, gate runs, or approval records.

#### Scenario: Viewer Q&A interrupts between sentences
- **WHEN** runtime answers a viewer cluster between approved sentences
- **THEN** the approved script version and approval record SHALL remain unchanged
- **AND** runtime SHALL resume from its stored next-sentence cursor.
