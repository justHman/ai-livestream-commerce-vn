# Multi-Session Batched TTS Runtime Specification

### Requirement: Provider-neutral external synthesis contract
The TTS service SHALL expose a provider-neutral single-request synthesis contract that does not require callers to know VieNeu, CUDA, model-internal tensors, or GPU batch construction.

#### Scenario: Backend synthesizes one speech chunk
- **GIVEN** a valid speech chunk request
- **WHEN** the backend calls `POST /v1/audio/speech`
- **THEN** the service SHALL accept provider-neutral fields including input text, voice profile, style, session/chunk identity, priority, and response format
- **AND** SHALL return only the audio/result belonging to that HTTP request.

#### Scenario: Provider is replaced
- **GIVEN** a future provider implements the service `TTSProvider` contract
- **WHEN** the active provider changes
- **THEN** backend callers SHALL NOT require a provider-specific request shape change for ordinary synthesis.

### Requirement: Transparent service-owned batching
The TTS service SHALL own GPU batching globally; backend callers SHALL NOT need to construct batches or invoke a public batch endpoint to obtain batching benefits.

#### Scenario: Concurrent single requests arrive
- **GIVEN** multiple sessions have concurrent `POST /v1/audio/speech` requests
- **WHEN** those requests are compatible for the active provider
- **THEN** the service MAY execute them together in one provider batch
- **AND** each caller SHALL still observe an independent single-request response.

#### Scenario: Backend does not expose batch knowledge
- **WHEN** backend integration code is reviewed
- **THEN** it SHALL NOT calculate provider GPU batch size or call a required `/v1/audio/speech/batch` endpoint.

### Requirement: Continuous admission with static in-flight batches
The scheduler SHALL accept requests continuously while treating each dispatched provider batch as immutable once inference begins.

#### Scenario: Requests arrive during inference
- **GIVEN** provider batch B1 is already in flight
- **WHEN** new requests arrive
- **THEN** they SHALL enter the pending scheduler population
- **AND** SHALL NOT be inserted into B1.

#### Scenario: Pending requests exist when batch completes
- **GIVEN** B1 completes and compatible pending work exists
- **WHEN** the provider becomes available
- **THEN** the scheduler SHALL form and dispatch the next batch without waiting for an unnecessary idle coalescing window.

### Requirement: Idle coalescing window
For a native-batch provider, the scheduler SHALL support a short configurable coalescing window when the provider is idle and the pending population was previously empty.

#### Scenario: Low-load first request
- **GIVEN** the provider is idle and the queue was empty
- **WHEN** the first compatible request arrives
- **THEN** the scheduler SHALL wait no longer than `coalesce_window_ms` for additional compatible work unless an earlier dispatch rule applies.

#### Scenario: Batch fills before window expires
- **GIVEN** a coalescing window is active
- **WHEN** compatible requests reach effective `max_batch_size`
- **THEN** the scheduler SHALL dispatch immediately without waiting for the remaining window.

### Requirement: Configurable GPU batch bound
The scheduler SHALL bound every provider batch by the provider capability and configured service limit.

#### Scenario: VieNeu default batch limit
- **GIVEN** VieNeu advertises native batching and a maximum capacity at least 32
- **WHEN** the default service configuration is used
- **THEN** effective `max_batch_size` SHALL be no greater than 32 unless explicitly configured and benchmarked otherwise.

#### Scenario: Provider advertises a smaller limit
- **WHEN** provider `max_batch_size` is smaller than the service configured maximum
- **THEN** the scheduler SHALL use the provider limit.

### Requirement: Provider-defined batch compatibility
The generic scheduler SHALL delegate provider-specific batch-compatibility decisions to the active provider rather than hard-coding VieNeu voice/model rules.

#### Scenario: Compatible requests
- **GIVEN** two requests have the same provider-defined `batch_key`
- **WHEN** scheduling forms a batch
- **THEN** they MAY be selected together subject to fairness, priority, deadline, and capacity constraints.

#### Scenario: Incompatible generation settings
- **GIVEN** two requests have provider settings that cannot share one provider batch
- **WHEN** their `batch_key` values differ
- **THEN** the scheduler SHALL NOT place them in the same provider invocation.

### Requirement: Provider abstraction
The active synthesis runtime SHALL be accessed through one `TTSProvider` abstraction that declares capabilities, batching semantics, voice enrollment, and synthesis behavior.

#### Scenario: Scheduler invokes provider
- **WHEN** the scheduler dispatches work
- **THEN** it SHALL call provider interfaces rather than VieNeu modules directly.

#### Scenario: Provider has no useful native batch
- **GIVEN** a provider advertises `supports_native_batch=false`
- **WHEN** requests are served
- **THEN** the scheduler SHALL use effective batch capacity one and SHALL NOT impose batching coalescence solely for throughput.

### Requirement: VieNeu v3 Turbo production provider
The first production provider SHALL use VieNeu v3 Turbo through the Python SDK/runtime and SHALL remove vLLM/vLLM-Omni from the active TTS execution path.

#### Scenario: GPU service starts
- **GIVEN** compatible CUDA/PyTorch is available
- **WHEN** the VieNeu provider initializes in normal auto mode
- **THEN** it SHALL use the VieNeu v3 Turbo GPU/PyTorch backend
- **AND** readiness SHALL report the selected provider/model/backend.

#### Scenario: Active runtime audit
- **WHEN** the change is ready for closeout
- **THEN** active TTS production startup/configuration SHALL NOT require vLLM-Omni to synthesize speech.

### Requirement: CPU functional fallback
The VieNeu provider SHALL retain a functional ONNX/CPU fallback while treating CPU batch inference as sequential compatibility rather than a GPU-throughput feature.

#### Scenario: CUDA is unavailable
- **WHEN** VieNeu initializes on a host without the required CUDA runtime
- **THEN** it SHALL be able to use the supported ONNX/CPU backend
- **AND** the scheduler SHALL use effective batch capacity one unless the provider later advertises native CPU batching.

#### Scenario: CPU verification
- **WHEN** CPU fallback tests run
- **THEN** single synthesis and API compatibility SHALL be verified
- **AND** a batch-size performance sweep SHALL NOT be required for acceptance.

### Requirement: Exact VieNeu dependency pin
The service SHALL pin the exact VieNeu package/model compatibility used by the provider adapter and SHALL NOT rely on a floating mutable upstream `main` contract.

#### Scenario: Build dependency is resolved
- **WHEN** the TTS image is built
- **THEN** the lock/build metadata SHALL identify the exact supported VieNeu package/revision and compatible PyTorch/transformers constraints.

#### Scenario: Upstream adapter surface changes
- **GIVEN** an installed VieNeu version no longer satisfies required provider contract checks
- **WHEN** the service starts
- **THEN** readiness SHALL fail rather than silently serving mixed voices through an incompatible path.

### Requirement: Isolated VieNeu internal batch-engine usage
Any use of VieNeu's lower-level v3 Turbo batch engine SHALL be isolated inside the VieNeu provider adapter.

#### Scenario: Repository import audit
- **WHEN** production source is searched for `v3_turbo_serve` or `V3TurboBatchEngine`
- **THEN** active direct usage SHALL exist only inside the designated VieNeu provider adapter and its provider-focused tests.

#### Scenario: Generic scheduler handles a VieNeu batch
- **WHEN** a VieNeu batch is dispatched
- **THEN** generic scheduler code SHALL pass provider requests to `TTSProvider`
- **AND** SHALL NOT access `speaker_emb`, `ref_codes`, or VieNeu engine classes.

### Requirement: Mixed-voice VieNeu GPU batches
The VieNeu provider SHALL support batching requests with different preset or cloned voice profiles in one provider batch when their provider-level generation settings are otherwise compatible.

#### Scenario: Two preset voices
- **GIVEN** compatible requests from different sessions using two preset voice profiles
- **WHEN** they are batched by the VieNeu provider
- **THEN** each provider row SHALL use its own speaker conditioning
- **AND** each returned waveform SHALL correspond to the requested voice.

#### Scenario: Two cloned voices
- **GIVEN** compatible requests using two different tenant-scoped cloned voice profiles
- **WHEN** they share one GPU batch
- **THEN** the provider SHALL use the correct per-request cloned speaker representation for each row
- **AND** SHALL not require singleton batches solely because the voices differ.

### Requirement: Mixed-style VieNeu GPU batches
The VieNeu provider SHALL support per-request reading style in a mixed batch when the pinned engine contract supports per-request style conditioning.

#### Scenario: Natural and storytelling requests share a batch
- **GIVEN** otherwise-compatible requests with different supported style IDs
- **WHEN** provider batch execution occurs
- **THEN** each row SHALL receive its requested style
- **AND** returned request identity SHALL remain exact.

### Requirement: Tenant-scoped opaque voice profiles
Backend callers SHALL reference voices through opaque `voice_profile_id` values scoped to tenant/account ownership rather than provider-global display names.

#### Scenario: Same display name in two tenants
- **GIVEN** Tenant A and Tenant B each create a voice named `Giọng của tôi`
- **WHEN** profiles are stored
- **THEN** they SHALL have distinct opaque IDs
- **AND** one tenant SHALL not resolve the other's profile through display name collision.

#### Scenario: Synthesis request uses a profile
- **WHEN** a request supplies `voice_profile_id`
- **THEN** the service SHALL resolve the profile only after tenant authorization
- **AND** SHALL keep provider-specific speaker data internal.

### Requirement: Preset and cloned voices share one caller abstraction
Preset voices and cloned voices SHALL be addressable through the same external `voice_profile_id` field.

#### Scenario: Caller switches from preset to clone
- **WHEN** a session changes its selected voice from a preset profile to an enrolled clone
- **THEN** the backend synthesis request schema SHALL remain unchanged except for the profile ID.

### Requirement: Voice enrollment
The service SHALL support one-time enrollment of a bounded reference WAV into a reusable cloned voice profile.

#### Scenario: Enroll valid reference audio
- **GIVEN** authorized reference WAV data within configured size/duration/media constraints
- **WHEN** the client calls the voice enrollment API
- **THEN** the provider SHALL derive and persist the reusable provider representation
- **AND** return an opaque `voice_profile_id`.

#### Scenario: Subsequent synthesis
- **GIVEN** an enrolled profile
- **WHEN** later synthesis requests reference it
- **THEN** the service SHALL reuse the stored provider representation
- **AND** SHALL NOT re-run reference enrollment for every speech chunk.

### Requirement: Restart-safe voice profile persistence
Cloned voice profiles SHALL survive TTS service restart through a configured persistent `VoiceProfileStore`.

#### Scenario: TTS task restarts
- **GIVEN** an enrolled persistent profile
- **WHEN** the service restarts and receives a synthesis request for that profile
- **THEN** it SHALL reload the profile from persistent storage or cache population
- **AND** SHALL not require the user to upload the reference WAV again.

#### Scenario: Local development
- **WHEN** a local file-backed store is configured
- **THEN** the same repository abstraction SHALL be used without changing synthesis API semantics.

### Requirement: Voice profile deletion and isolation
Voice profile deletion SHALL remove future usability of that profile without affecting unrelated tenants/profiles.

#### Scenario: Tenant deletes cloned profile
- **WHEN** an authorized deletion succeeds
- **THEN** future synthesis with that ID SHALL fail deterministically
- **AND** unrelated cached/persisted profiles SHALL remain available.

### Requirement: Expressive cue capability discovery
The provider-neutral capability API SHALL describe supported expressive cues without requiring backend code to infer them from provider documentation.

#### Scenario: VieNeu v3 Turbo is active
- **WHEN** capabilities are requested
- **THEN** the service SHALL expose supported project/provider cue identifiers corresponding to the pinned VieNeu capability set, including the currently supported laugh, sigh, and throat-clear semantics.

#### Scenario: Provider does not support a cue
- **WHEN** a synthesis request requires an unsupported cue under the selected provider/profile contract
- **THEN** the service SHALL fail validation according to the stable capability/error contract rather than silently changing approved text.

### Requirement: Session-aware fair scheduling
Within a priority tier, the scheduler SHALL prevent one session with deep queued work from monopolizing all batch slots while other eligible sessions wait.

#### Scenario: One long script and two new sessions
- **GIVEN** Session A has many pending chunks and Sessions B/C each have pending chunks at the same priority
- **WHEN** the next batch is selected
- **THEN** the fairness algorithm SHALL give B/C eligible opportunities rather than filling every slot from A.

#### Scenario: Same-priority sustained load
- **GIVEN** several active same-priority sessions continually submit work
- **WHEN** the scheduler runs over time
- **THEN** no eligible session SHALL be indefinitely starved by another same-priority session.

### Requirement: Per-session FIFO scheduling
For requests within the same session and priority, scheduler selection SHALL preserve submitted chunk order unless a future explicit protocol changes this rule.

#### Scenario: Session has chunks 10, 11, and 12 pending
- **WHEN** the session receives scheduler slots
- **THEN** chunk 10 SHALL be selected before 11 and 11 before 12.

### Requirement: Generic priority tiers
The synthesis contract SHALL support provider-neutral priority metadata without coupling Change T to `/ws/platform`, Director, or Q&A semantics.

#### Scenario: High and normal requests are pending
- **WHEN** the scheduler selects eligible work
- **THEN** high-priority work SHALL be considered before normal-priority work subject to fairness/aging and non-preemptive in-flight behavior.

#### Scenario: Batch already in flight
- **GIVEN** a normal-priority provider batch is already executing
- **WHEN** a high-priority request arrives
- **THEN** the current static provider batch SHALL not be preempted
- **AND** the high-priority request SHALL be eligible for the next dispatch.

### Requirement: Normal-priority starvation protection
Sustained high-priority traffic SHALL NOT cause unbounded starvation of accepted normal-priority requests.

#### Scenario: Continuous high-priority arrivals
- **GIVEN** normal requests were accepted before/during sustained high-priority traffic
- **WHEN** their configured aging/fairness bound is reached
- **THEN** the scheduler SHALL make progress on eligible normal work rather than postponing it indefinitely.

### Requirement: Global queue bound
The scheduler SHALL impose a configured global pending-request limit.

#### Scenario: Global queue is full
- **WHEN** another request attempts admission
- **THEN** the service SHALL reject it with stable overload semantics
- **AND** SHALL NOT grow pending memory without bound.

### Requirement: Per-session queue bound
The scheduler SHALL impose a configured per-session pending-request limit so one backend/session cannot consume the whole service queue.

#### Scenario: Session exceeds prefetch allowance
- **WHEN** a session submits beyond its configured pending limit
- **THEN** additional requests SHALL receive stable backpressure/overload semantics
- **AND** other sessions SHALL retain admission capacity subject to the global bound.

### Requirement: Request deadline handling
Accepted requests SHALL carry or receive an effective scheduling deadline and SHALL not remain pending indefinitely.

#### Scenario: Deadline is near while provider is idle
- **WHEN** waiting the full coalescing window would violate the request deadline
- **THEN** the scheduler SHALL dispatch eligible work earlier when execution capacity is available.

#### Scenario: Request expires while queued
- **WHEN** a request cannot be dispatched before its effective deadline
- **THEN** it SHALL fail with a stable deadline outcome
- **AND** SHALL be removed from pending scheduler state.

### Requirement: Disconnect cancellation
The service SHALL remove a disconnected/cancelled request from pending work when possible and SHALL safely discard its result if cancellation occurs after static batch dispatch.

#### Scenario: Caller disconnects before dispatch
- **WHEN** cancellation is observed while the request is pending
- **THEN** it SHALL be removed and SHALL not consume a provider batch slot.

#### Scenario: Caller disconnects after dispatch
- **WHEN** the request is already part of an in-flight static provider batch
- **THEN** sibling requests SHALL continue
- **AND** the cancelled request result SHALL be discarded instead of routed to another caller.

### Requirement: Cross-session result isolation
Batching SHALL never change which synthesized waveform belongs to which request/session.

#### Scenario: Mixed batch completes
- **GIVEN** one provider batch contains requests from Sessions A, B, and C
- **WHEN** provider results return
- **THEN** each waveform SHALL resolve only the immutable request identity that produced it
- **AND** zero cross-session audio routing SHALL be tolerated.

### Requirement: No accepted-result duplication or loss
Each accepted, non-cancelled synthesis request that completes successfully SHALL resolve exactly once with one result.

#### Scenario: Provider returns batch results
- **WHEN** results are mapped back after scheduler/provider reordering
- **THEN** every successful accepted request SHALL have exactly one audio result
- **AND** no result SHALL be duplicated or silently dropped.

### Requirement: Consumer-visible chunk identity preservation
The TTS service SHALL preserve request/session/utterance/chunk identity needed by backend observability and ordered playback without taking ownership of avatar playback ordering.

#### Scenario: Same session requests multiple chunks concurrently
- **WHEN** audio responses complete through different provider batches
- **THEN** each response/tracing record SHALL retain the original chunk identity
- **AND** backend playback ordering MAY use that identity independently of TTS execution order.

### Requirement: Provider failure isolation
A provider failure affecting one request or batch SHALL be surfaced without corrupting unrelated queued or completed requests.

#### Scenario: One batch fails
- **WHEN** the provider batch invocation raises an error
- **THEN** affected requests SHALL receive deterministic failure outcomes
- **AND** scheduler state for later unrelated work SHALL remain coherent and continue if the provider remains ready.

#### Scenario: Invalid one-request input detected before provider batch
- **WHEN** request validation fails
- **THEN** that request SHALL be rejected before batching
- **AND** compatible valid sibling requests SHALL remain eligible.

### Requirement: Readiness reflects real model/provider state
`GET /ready` SHALL distinguish process liveness from ability to synthesize using the selected provider/model and scheduling runtime.

#### Scenario: Model is still loading
- **WHEN** `/health` is called
- **THEN** liveness MAY be healthy
- **BUT WHEN** `/ready` is called
- **THEN** readiness SHALL remain false until provider/model/profile-store/scheduler startup requirements are satisfied.

### Requirement: Provider capability endpoint
The service SHALL expose stable capability metadata required by backend/authoring readiness checks without exposing provider-internal tensors.

#### Scenario: Query capabilities
- **WHEN** a caller requests `/v1/audio/capabilities`
- **THEN** it SHALL receive selected provider/model revision, sample rate, styles, expressive cues, cloning availability, response formats, native-batch capability, and relevant limits.

### Requirement: Accurate v3 Turbo audio format metadata
The VieNeu v3 Turbo provider SHALL represent its actual audio sample rate/format and SHALL not retain stale VieNeu-v2 audio assumptions.

#### Scenario: VieNeu v3 Turbo returns audio
- **WHEN** synthesis succeeds
- **THEN** response encoding/metadata SHALL use the configured v3 Turbo 48 kHz waveform semantics unless explicitly converted by a documented output adapter.

### Requirement: Content-private observability
Normal metrics/logs SHALL not emit full synthesis text, raw reference audio, speaker embeddings, or reference codes.

#### Scenario: Scheduler metrics are emitted
- **WHEN** request/batch metrics are recorded
- **THEN** they SHALL use bounded labels such as provider/backend/priority/outcome
- **AND** raw text/session IDs SHALL not be used as unbounded metric labels.

### Requirement: Scheduler performance metrics
The service SHALL record enough metrics to measure batching efficiency, queueing, and capacity.

#### Scenario: GPU batch completes
- **WHEN** a provider batch finishes
- **THEN** metrics SHALL include batch size/fill, queue wait, inference wall time, produced audio seconds, RTF or equivalent throughput, and provider/backend identity.

### Requirement: Direct provider benchmark
Change T SHALL include a reproducible direct-provider benchmark on the same hardware used for service overhead comparisons.

#### Scenario: GPU benchmark sweep
- **WHEN** VieNeu GPU performance is evaluated
- **THEN** the benchmark SHALL record at least batch sizes 1, 4, 8, 16, and 32 where supported
- **AND** record item count, wall time, audio seconds, RTF, realtime factor, and items/sec.

#### Scenario: CPU benchmark
- **WHEN** CPU fallback is verified
- **THEN** single/compatibility smoke SHALL be sufficient for Change T performance acceptance because upstream CPU batch execution is sequential.

### Requirement: Service throughput acceptance relative to direct provider
At saturated compatible GPU load, the full HTTP scheduler/service path SHOULD preserve at least 80% of direct-provider audio-seconds-per-wall-second on the same host, provider revision, corpus, and effective generation configuration.

#### Scenario: Saturated multi-session benchmark
- **GIVEN** a direct provider throughput baseline
- **WHEN** the same workload is driven through real concurrent HTTP requests and service batching
- **THEN** measured service throughput SHALL be compared against that baseline
- **AND** a result below 80% SHALL block performance acceptance unless the OpenSpec acceptance rule is explicitly revised with measured justification before closeout.

### Requirement: Multi-session load matrix
Change T SHALL verify correctness and scheduling behavior with concurrent independent sessions, not only many texts from one session.

#### Scenario: Concurrent session sweep
- **WHEN** load tests run
- **THEN** they SHALL cover practical concurrency levels including 1, 2, 4, 8, 16, and 32 sessions where the benchmark host can sustain them
- **AND** record queue and throughput metrics per run.

### Requirement: Mixed voice load verification
Load tests SHALL include same-voice, mixed preset-voice, and mixed cloned-voice traffic.

#### Scenario: Mixed cloned voice benchmark
- **GIVEN** multiple enrolled test voice profiles
- **WHEN** concurrent sessions synthesize with those profiles
- **THEN** the test SHALL verify correct voice/profile routing and measure batch fill/throughput without forcing singleton batches merely because profile IDs differ.

### Requirement: Fairness and starvation load verification
Load tests SHALL include a dominant long-session workload plus newly arriving sessions and SHALL detect starvation.

#### Scenario: Long script dominates queue
- **GIVEN** Session A continuously has many chunks queued
- **WHEN** Sessions B/C begin sending at the same priority
- **THEN** B/C SHALL receive bounded progress according to the configured fairness policy
- **AND** the test SHALL fail on indefinite starvation.

### Requirement: Burst and continuous-arrival verification
The service SHALL be tested under both bursty and staggered continuous request arrival.

#### Scenario: Simultaneous burst
- **WHEN** many sessions submit at approximately the same time
- **THEN** batching/backpressure SHALL remain bounded and routing correct.

#### Scenario: Continuous arrival
- **WHEN** sessions submit requests over an extended interval while batches remain active
- **THEN** pending work SHALL flow through successive batches without queue corruption or provider idle gaps caused solely by scheduler logic when backlog exists.

### Requirement: Soak and resource stability
Change T SHALL include a long-running multi-session soak test sufficient to detect queue growth, memory leaks, VRAM growth, and fairness degradation.

#### Scenario: Soak run completes
- **WHEN** the configured soak workload finishes
- **THEN** pending queues SHALL return to baseline
- **AND** RAM/VRAM SHALL not show unexplained unbounded growth
- **AND** no session routing/fairness correctness failure SHALL have occurred.

### Requirement: No Change A reopening
Change T SHALL integrate with completed Change A output without changing TextChunker segmentation or reopening Change A benchmark acceptance.

#### Scenario: Integration smoke
- **GIVEN** Change A produces canonical `TextChunk` output
- **WHEN** those chunks are synthesized through Change T
- **THEN** audio SHALL be produced through ordinary per-chunk requests
- **AND** the test SHALL be treated as integration verification, not a new Change A benchmark gate.

### Requirement: Change B runtime prerequisite
Change B production speech integration SHALL target the stable Change T contract and SHALL not depend directly on VieNeu SDK internals.

#### Scenario: Change B begins runtime integration
- **GIVEN** Change T has passed provider, scheduler, contract, and multi-session acceptance
- **WHEN** approved scripts are connected to speech runtime
- **THEN** Change B/backend SHALL call the provider-neutral per-chunk TTS service contract after Change A segmentation.

### Requirement: Future Q&A workflow excluded
Change T SHALL not implement `/ws/platform`, viewer-message prioritization logic, Director Q&A selection, or script interruption policy.

#### Scenario: Priority field exists
- **WHEN** Change T exposes `priority`
- **THEN** the field SHALL remain generic scheduler metadata
- **AND** the reason a future request is high priority SHALL be defined by a later capability/change.

### Requirement: Active legacy runtime removal
The final TTS runtime SHALL not retain a second active vLLM-Omni/VieNeu-v2 execution path solely for migration convenience.

#### Scenario: Pre-closeout architecture audit
- **WHEN** the repository is audited before Change T closeout
- **THEN** active startup/runtime code, Docker entrypoint, config, and docs SHALL describe the provider-neutral VieNeu-v3-Turbo implementation
- **AND** stale active vLLM-Omni TTS execution assumptions SHALL be removed or clearly historical only.

### Requirement: Strict verification before closeout
Change T SHALL not be considered complete until focused tests, provider contracts, multi-session load/soak evidence, static checks, and OpenSpec validation pass.

#### Scenario: Closeout gate
- **WHEN** implementation is proposed for completion
- **THEN** provider/unit/contract/integration/load tests, formatting/static checks, `git diff --check`, and strict OpenSpec validation for `multi-session-batched-tts-runtime` SHALL pass
- **AND** the measured performance/capacity report SHALL be recorded.
