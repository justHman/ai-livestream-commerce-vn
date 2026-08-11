## ADDED Requirements

### Requirement: Source-agnostic text segmentation

The backend SHALL provide one speech text chunking capability that accepts arbitrary text fragments without requiring or inferring whether the source is an LLM stream, a human-authored script, or another producer.

#### Scenario: Complete script is supplied in one call

- **GIVEN** a complete Vietnamese script is available before synthesis
- **WHEN** the caller feeds the complete script and finalizes the chunker
- **THEN** the chunker SHALL emit ordered `TextChunk` values using the same content-boundary policy used for incremental input
- **AND** the caller SHALL NOT need to fake token-by-token streaming

#### Scenario: Incremental fragments are supplied

- **GIVEN** the same speech text arrives as arbitrary incremental fragments
- **WHEN** the caller feeds the fragments in order
- **THEN** the chunker SHALL accept fragment sizes ranging from a single character to multiple sentences
- **AND** SHALL NOT depend on punctuation being the final character of an individual fragment

### Requirement: Exact text preservation

The chunker SHALL preserve the original input text exactly and SHALL only segment it, never rewrite, normalize, omit, duplicate, or reorder content.

#### Scenario: Reconstructing input from chunks

- **WHEN** all emitted chunk text and the final buffered remainder are concatenated in sequence order
- **THEN** the result SHALL equal the exact concatenation of all non-empty input fragments

### Requirement: Fragmentation invariance

Content-based segmentation SHALL be invariant to input fragmentation when policy, runtime hints, concatenated text, and externally requested flushes are identical.

#### Scenario: Full text versus fragmented text

- **GIVEN** one run receives a complete text in one `feed()` call
- **AND** another run receives the same text across arbitrary fragment boundaries
- **AND** neither run receives an external realtime deadline flush
- **WHEN** both runs finalize under identical policy and runtime hints
- **THEN** their emitted chunk texts and ordering SHALL be equivalent

#### Scenario: Realtime deadline is the explicit exception

- **GIVEN** incremental text has been buffered
- **WHEN** the realtime controller explicitly requests a latency-deadline flush before more text arrives
- **THEN** the chunker MAY commit an earlier valid boundary than the no-deadline full-text run
- **AND** SHALL preserve all correctness invariants

### Requirement: Vietnamese boundary scoring

The adaptive policy SHALL rank candidate speech boundaries using deterministic Vietnamese-oriented linguistic features rather than raw character count alone.

#### Scenario: Punctuation exists inside a fragment

- **GIVEN** a fragment contains one or more sentence/clause punctuation marks before its final character
- **WHEN** the chunker evaluates the accumulated buffer
- **THEN** those internal punctuation positions SHALL be considered as candidate boundaries

#### Scenario: Natural boundary is available

- **GIVEN** multiple safe candidate boundaries exist around the preferred speech duration
- **WHEN** the chunker selects a split
- **THEN** stronger paragraph/sentence/clause boundaries SHALL outrank a plain whitespace split when hard constraints are otherwise satisfied

#### Scenario: Protected span is near the target

- **GIVEN** a candidate would split a decimal/grouped number, currency/percentage form, URL/email, acronym, SKU-like token, or another protected span
- **WHEN** a nearby safe candidate exists
- **THEN** the protected-span candidate SHALL NOT be selected

### Requirement: Estimated speech duration

Adaptive chunk sizing SHALL use estimated spoken duration as its primary soft size signal and SHALL use raw character thresholds only as quality/safety/fallback signals.

#### Scenario: Text contains a compact written form with long spoken form

- **GIVEN** text contains a number, price, percentage, acronym, or similar compact written form
- **WHEN** candidate chunk duration is estimated
- **THEN** the estimator SHALL account for expected spoken complexity rather than treating raw character count as the sole duration proxy

#### Scenario: Duration estimation is unavailable

- **WHEN** adaptive duration estimation cannot produce a valid estimate for an utterance
- **THEN** the chunker SHALL fall back deterministically to the fixed character policy
- **AND** SHALL not lose or reorder text

### Requirement: Adaptive runtime policy

The adaptive policy SHALL adjust soft chunk targets using source-agnostic runtime hints while preserving all hard correctness constraints.

#### Scenario: Speech has not started and startup is late

- **GIVEN** no first audio has been observed
- **AND** `speech_start_elapsed_ms` is high
- **WHEN** multiple linguistically valid boundaries exist
- **THEN** the policy SHALL prefer an earlier valid boundary than healthy steady state would prefer

#### Scenario: Playback buffer is healthy

- **GIVEN** playback buffer is above the healthy watermark
- **WHEN** multiple safe boundaries are available
- **THEN** the policy SHALL prefer a longer coherent phrase near the steady-state duration target

#### Scenario: Playback approaches starvation

- **GIVEN** playback buffer is below the starvation watermark or TTS latency/RTF has degraded
- **WHEN** a safe earlier boundary is available
- **THEN** the policy SHALL lower its soft duration target and prefer that earlier boundary

### Requirement: Real streaming deadline

Realtime incremental input SHALL support a true buffer deadline that can fire while synchronous upstream LLM iteration is idle.

#### Scenario: Upstream emits no next delta

- **GIVEN** speakable text is buffered
- **AND** the synchronous LLM iterator does not yield another delta before the configured deadline
- **WHEN** the deadline expires
- **THEN** orchestration SHALL invoke an explicit latency-deadline flush without waiting for another LLM yield

#### Scenario: Complete script path has no realtime wait

- **GIVEN** complete script text is already available
- **WHEN** it is segmented for TTS
- **THEN** no realtime deadline thread/queue SHALL be required to make progress

### Requirement: Buffer age excludes TTFT

The buffer deadline clock SHALL begin when the first non-empty text enters an empty buffer and SHALL not include time spent waiting for the first upstream text.

#### Scenario: Long LLM TTFT precedes first delta

- **GIVEN** the utterance waits longer than `flush_timeout_ms` before its first LLM delta
- **WHEN** that first delta enters an empty chunker buffer
- **THEN** its buffer age SHALL begin at zero for deadline purposes
- **AND** SHALL NOT flush immediately solely because TTFT exceeded the deadline

### Requirement: Character settings have explicit semantics

Character thresholds SHALL remain deterministic compatibility/safety controls under the adaptive policy.

#### Scenario: Minimum quality floor

- **WHEN** an automatic content or deadline split is considered below `min_chars`
- **THEN** the chunker SHALL continue buffering unless finalization, cancellation semantics, or the absolute hard cap requires otherwise

#### Scenario: Target character fallback

- **GIVEN** duration estimates are unavailable or multiple candidate scores are otherwise equivalent
- **WHEN** a preferred boundary must be selected
- **THEN** `target_chars` SHALL participate as a deterministic fallback/tie-break signal

#### Scenario: Hard maximum with a large delta

- **GIVEN** appending a multi-character fragment would make the buffered text exceed `max_chars`
- **WHEN** the buffer is drained
- **THEN** every non-final automatically emitted chunk SHALL contain at most `max_chars`
- **AND** one `feed()` call MAY emit multiple chunks

### Requirement: Canonical TextChunk type

The backend SHALL expose exactly one canonical `TextChunk` class for speech-text chunks.

#### Scenario: Legacy render import remains during migration

- **WHEN** existing code imports `TextChunk` through the render-window compatibility path
- **THEN** that import SHALL resolve to the same class object as the canonical speech-chunking `TextChunk`
- **AND** new code SHALL use the canonical import path

### Requirement: Exactly-once normal finality

Normal utterance completion SHALL produce exactly one terminal final marker through text, audio, and video stages, while errors and cancellation SHALL NOT fabricate successful completion.

#### Scenario: Normal utterance completes

- **WHEN** an utterance completes normally and produces speech audio/video
- **THEN** exactly the last `TextChunk` SHALL have `is_final=True`
- **AND** exactly the last corresponding `AudioWindow` SHALL have `is_final=True`
- **AND** exactly the last corresponding `VideoWindow` SHALL have `is_final=True`

#### Scenario: Utterance is cancelled or errors

- **WHEN** upstream generation, TTS, playback preparation, or caller cancellation terminates the utterance abnormally
- **THEN** the pipeline SHALL use its error/cancel completion path
- **AND** SHALL NOT emit a normal-success final marker solely to close the stream

### Requirement: Bounded realtime backpressure

Realtime LLM text production SHALL be bounded so slow TTS consumption cannot create unbounded in-memory text accumulation.

#### Scenario: TTS is slower than LLM text production

- **WHEN** the bounded streaming queue reaches capacity
- **THEN** the producer SHALL block or otherwise apply backpressure
- **AND** SHALL NOT silently drop, duplicate, or reorder deltas

#### Scenario: Producer raises an exception

- **WHEN** the LLM streaming producer raises an exception
- **THEN** the exception SHALL be propagated to the consumer/utterance error path
- **AND** buffered text SHALL not be falsely marked as a normally completed utterance

### Requirement: Chunk decision observability

The runtime SHALL expose content-free-by-default metrics sufficient to explain and benchmark chunk decisions.

#### Scenario: Chunk is emitted

- **WHEN** any `TextChunk` is emitted
- **THEN** observability SHALL record its sequence, character length, estimated speech duration, and decision reason
- **AND** SHALL indicate hard-max/protected-span fallback events
- **AND** SHALL NOT log the chunk text by default

#### Scenario: VieNeu produces audio

- **WHEN** TTS timing and audio duration are available
- **THEN** telemetry SHALL record first-audio/synthesis latency, generated audio duration, and RTF in a form usable to update bounded runtime hints

### Requirement: Deterministic fixed-policy rollback

The backend SHALL retain a deterministic fixed-policy mode while the adaptive policy is introduced and benchmarked.

#### Scenario: Adaptive analysis fails

- **WHEN** adaptive candidate analysis fails for an utterance
- **THEN** the utterance SHALL fall back to fixed segmentation without dropping/reordering text
- **AND** SHALL emit an observability signal describing the fallback

#### Scenario: Adaptive benchmark has not passed

- **GIVEN** VieNeu benchmark acceptance has not passed
- **WHEN** the service starts with project-default configuration
- **THEN** the fixed policy SHALL remain the safe default

### Requirement: VieNeu benchmark gate

The adaptive policy SHALL not become the intended default and the downstream approved-script change SHALL not start until a fixed-versus-adaptive VieNeu benchmark passes correctness, prosody, and latency gates.

#### Scenario: Selecting the adaptive candidate

- **GIVEN** fixed-baseline and adaptive-candidate runs use the same representative Vietnamese benchmark corpus
- **WHEN** benchmark results are evaluated
- **THEN** all correctness invariants SHALL have zero failures
- **AND** only candidates with no material human-reviewed prosody regression SHALL be eligible
- **AND** the eligible candidate with the lowest median TTFA SHALL be selected
- **AND** its TTFA p95 SHALL not regress by more than 5 percent versus fixed baseline

#### Scenario: No adaptive candidate passes

- **WHEN** no candidate satisfies correctness, human prosody non-regression, and TTFA constraints
- **THEN** the change SHALL remain NOT PASS
- **AND** fixed segmentation SHALL remain the default
- **AND** `approved-script-authoring-pipeline` SHALL remain blocked
