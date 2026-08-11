# Design: Adaptive Speech Text Chunking

## Context

The current pipeline receives incremental text from `LLMEngine.stream_chunks()`, feeds `token.text` into `TextChunker`, and sends emitted text to TTS. The existing chunker uses character thresholds, punctuation-at-end-of-delta, and a clock value initialized when the chunker is created. The reviewed orchestration checks timeout only while iterating the synchronous LLM generator, so no true deadline fires during an upstream stall. A complete script is a valid future input to the same speech path, so source identity must not be part of segmentation behavior.

This change intentionally separates three concerns:

1. **Content segmentation** — where the text can be spoken naturally.
2. **Realtime control** — when waiting must stop because speech startup/playback would otherwise stall.
3. **TTS feedback** — how observed VieNeu latency and playback health adjust soft targets without violating deterministic correctness.

## Goals

- One `TextChunker` implementation for arbitrary incremental fragments and complete scripts.
- Exact text preservation and deterministic ordering.
- Natural Vietnamese phrase boundaries as the primary content signal.
- Estimated spoken duration as the primary soft size signal.
- Lower first-audio latency without measurable prosody regression on VieNeu.
- A real streaming deadline independent of whether another LLM delta arrives.
- Exactly-once normal finality across text, audio, and video windows.
- No new neural runtime dependency and no content moderation responsibility in this change.

## Non-goals

- Drafting, approving, moderating, spell-checking, or rewriting scripts.
- Detecting AI-generated text or profanity.
- Changing VieNeu model weights or training a boundary model.
- Making `TextChunker` aware of `LLM`, `script`, `human`, `product`, or `approval` source types.
- Removing the existing fixed-threshold behavior before the adaptive path has benchmark evidence and a rollback switch.

## Decision 1 — Source-agnostic public contract

`TextChunker` SHALL accept arbitrary text fragments. The caller may provide one LLM delta, many coalesced deltas, a sentence, a paragraph, or a full script. The chunker must not branch on source identity.

Conceptual API:

```python
chunker = TextChunker(
    session_id=session_id,
    utterance_id=utterance_id,
    policy=policy,
    clock=clock,
)

chunks = chunker.feed(text_fragment, runtime_hints=hints)
chunks += chunker.flush(reason="latency_deadline", runtime_hints=hints)
chunks += chunker.finalize(runtime_hints=hints)
```

`flush()` is an explicit caller action. It does not imply that the chunker owns a timer. `feed(full_script)` followed by `finalize()` is the complete-script path; it does not require fake tokenization or artificial sleeps.

### Fragmentation invariance

For identical concatenated input text, identical policy/runtime hints, and no externally requested latency flush, segmentation SHALL be independent of how the input was fragmented across `feed()` calls.

This is achieved by making content decisions from the accumulated buffer and a bounded decision horizon rather than from `token_text[-1]` or provider-specific delta shape.

Realtime deadline flushes are the explicit exception: an early caller-requested flush is allowed to commit a boundary that a full-context script would not need to commit yet.

## Decision 2 — Keep streaming deadlines outside segmentation

A chunker cannot provide a true 350 ms deadline while the caller is blocked inside a synchronous generator. The realtime LLM path therefore gains a bounded producer/controller around `LLMEngine.stream_chunks()`.

Conceptual flow:

```text
LLMEngine.stream_chunks()
        │ producer thread
        ▼
 bounded delta queue
        │ queue.get(timeout=remaining_deadline)
        ▼
 StreamOrchestrator consumer
        │
        ├─ delta → chunker.feed(...)
        ├─ deadline → chunker.flush(reason="latency_deadline")
        └─ EOF/error/cancel → explicit terminal handling
```

Constraints:

- Only the orchestrator consumer mutates `TextChunker`; the chunker remains single-threaded and lock-free.
- The queue is bounded so a slow TTS consumer exerts backpressure instead of allowing unbounded LLM text accumulation.
- The producer reports delta, EOF, and exception as typed queue events.
- Cancellation sets a stop event and closes the upstream generator when supported. Provider/network I/O must retain its own finite timeout because Python cannot safely force-kill a thread blocked in foreign I/O.
- The controller SHALL NOT start a separate queue/thread for complete-script input; full text is fed directly.

## Decision 3 — Buffer age starts when text enters an empty buffer

Replace constructor/last-flush age semantics with explicit buffer state:

```text
empty buffer
   │ first non-empty text appended
   ▼
buffer_started_at = now
   │
   ├─ content flush/finalize → buffer empty → None
   └─ remainder after split → remainder gets its own start time
```

LLM TTFT is not buffer age. If the utterance waits 2 seconds before the first delta, the first delta enters a zero-age buffer.

TTFT may still influence adaptive policy through `RuntimeHints.speech_start_elapsed_ms`; it must never cause the buffer deadline itself to appear expired before text existed.

## Decision 4 — Candidate boundaries operate on original text spans

The chunker never rewrites speech text. Analysis may use normalized shadow forms, but every output chunk is an exact slice of the original concatenated input. Joining all emitted chunk text plus the remaining buffer must reproduce the exact input.

Candidate boundary classes, from strongest to weakest content evidence:

1. paragraph or strong line boundary;
2. sentence punctuation (`.`, `!`, `?`, `…`) outside protected spans;
3. semicolon/colon;
4. comma or clause punctuation when the left phrase is sufficiently speakable;
5. Vietnamese clause/phrase cue near the duration target;
6. whitespace near the preferred duration/character target;
7. hard safety split when no natural candidate exists before the absolute cap.

The scorer SHALL avoid or strongly penalize splits inside protected spans such as:

- decimal/grouped numbers;
- currency and percentages;
- URLs/emails;
- common abbreviations/acronyms;
- SKU/product-code-like tokens;
- balanced quote/parenthesis regions when a nearby safer boundary exists.

Vietnamese cue words such as conjunctions or discourse markers are features, not unconditional split commands. The initial scorer is deterministic and rule-based.

## Decision 5 — Estimated speech duration is the primary soft size signal

Raw characters remain safety/compatibility information, but adaptive selection optimizes estimated spoken duration.

`SpeechDurationEstimator` is deterministic. It estimates a chunk from:

- Vietnamese syllable-like word units;
- punctuation pause weights;
- numbers and grouped numbers;
- currency and percentage forms;
- acronyms/English-like tokens that often take longer to pronounce;
- calibration coefficients derived from measured VieNeu audio duration.

It does not normalize the output text; it only estimates how long the original text is likely to speak.

Character settings have these post-change meanings:

- `min_chars`: quality/safety floor for normal automatic emission;
- `target_chars`: fallback/tie-break when duration estimates are ambiguous or calibration is unavailable;
- `max_chars`: absolute hard cap for every non-final automatic chunk, including multi-character deltas;
- `flush_timeout_ms`: streaming controller deadline, not a content target.

## Decision 6 — Adaptive policy has startup, steady, and starvation states

`RuntimeHints` contains only source-agnostic runtime facts:

```python
@dataclass(frozen=True)
class RuntimeHints:
    speech_start_elapsed_ms: float
    playback_buffer_ms: float | None
    tts_first_audio_ewma_ms: float | None
    tts_rtf_ewma: float | None
```

The chunker does not know why those values exist or whether the text came from an LLM or script.

Policy behavior:

### Startup

Before first audio is observed, prefer the earliest high-quality Vietnamese boundary that reaches the startup speech-duration floor. If `speech_start_elapsed_ms` is already high, the soft duration target moves downward, but linguistic-quality and hard-safety constraints still apply.

### Steady state

When playback buffer is healthy, prefer longer coherent phrases around the steady-state duration target to improve VieNeu prosody and reduce excessive TTS calls.

### Starvation protection

When playback buffer is below the starvation watermark or measured TTS RTF/first-audio latency worsens, move the soft target downward and accept an earlier natural boundary.

Adaptive hints may change which *valid* boundary wins. They may never violate text preservation, sequence ordering, max cap, protected-span safety, or finality.

## Decision 7 — `target_chars` is a fixed-policy concept; adaptive uses its own char-bias constant

`target_chars` lives in `FixedChunkPolicyConfig` and is used to:

- fix the finalize fallback boundary for an over-target pending buffer under the fixed policy;
- preserve a low-risk rollback path to fixed-threshold behavior.

It is NOT promoted into the adaptive objective. The adaptive config (`AdaptiveViPolicyConfig`) carries its own fixed neutral char-bias reference used only to rank equivalent whitespace candidates when speech-duration estimates are close, plus a deterministic fallback when the duration estimator is unavailable. `target_chars` never leaks into adaptive scoring.

The production default remains configurable between `fixed` and `adaptive_vi` until the benchmark gate passes. After PASS, `adaptive_vi` becomes the intended default; the fixed policy remains available for rollback during the first release window.

## Decision 8 — True hard max and multi-chunk drain

`feed()` may return zero, one, or many chunks. A single 250-character fragment must be drainable into multiple safe chunks.

Invariant:

```text
len(chunk.text) <= max_chars
```

for every non-final automatically emitted chunk.

If no acceptable natural boundary exists before `max_chars`, a forced split occurs at the best safe position at or before the cap. The decision reason is recorded as `hard_max` so forced-split rate can be benchmarked.

## Decision 9 — One canonical `TextChunk` in one cohesive package

Create one canonical `TextChunk` type. The final architecture is a single cohesive package — there is NO `text_chunker.py` facade file and NO parallel `speech_chunking/` package:

```text
backend/application/
└── text_chunker/
    ├── __init__.py                     # stable package exports
    ├── chunker.py                      # TextChunker state machine (feed/flush/finalize)
    ├── types.py                        # TextChunk, ChunkPolicy, RuntimeHints, FixedChunkPolicyConfig
    ├── boundaries.py                   # candidate extraction + protected spans
    ├── duration.py                     # SpeechDurationEstimator
    ├── policy.py                       # fixed + adaptive_vi strategies and configs
    └── telemetry.py                    # content-free chunk-decision telemetry
```

Consumers import the public API from the package root:

```python
from backend.application.text_chunker import TextChunker, TextChunk
```

`render/windows.py` keeps only render-stage concepts (`AudioWindow`, `VideoWindow`). It does not define or re-export `TextChunk` after migration.

### Full-script path uses the same TextChunker

There is exactly one chunking path. A complete script is fed into the same `TextChunker` and finalized; the only difference from realtime input is ingestion timing. `_speak_verbatim_sync` never constructs a `TextChunk` directly — finality comes from `finalize()`'s terminal position, normalized to exactly one final marker through the TTS seam.

### Realtime deadline ownership

`TextChunker` owns content/boundary state only. `StreamingControllerConfig` (render orchestration) carries `flush_timeout_ms`; the orchestrator computes the deadline from `TextChunker.buffer_started_at`/`buffer_age_ms` and calls `chunker.flush(reason="latency_deadline")` explicitly. The chunker has no timer or timeout knob.

### Policy strategies with typed configs

One `TextChunker` hosts injected segmentation strategies (`FixedPolicyStrategy` / `AdaptiveViPolicyStrategy`) behind a common protocol — no monolithic mode-switch. Configs are split: `FixedChunkPolicyConfig` (min/target/max chars) vs `AdaptiveViPolicyConfig` (speech-duration targets, scoring weights, hard safety constraints). `target_chars` never leaks into adaptive scoring; a fixed neutral char-bias constant lives in `AdaptiveViPolicyConfig`. The hard `max_chars` safety cap is an intentional invariant in both configs. When adaptive analysis fails, the chunker switches explicitly to the fixed strategy and stamps `fixed_fallback`.

### Centralized config ownership

`TEXT_CHUNK_*` env vars remain the source of truth in `AppConfig`. Callers build the typed configs from `AppConfig` once and pass them into `StreamOrchestrator` (`fixed_config`, `controller_config`); no duplicated `_DEFAULT_*` mirrors exist at the orchestration boundary.

### Finality stays exactly-once

`finalize()` owns the final marker. Normal completion with a remainder, completion with no remainder (the last already-emitted chunk is stamped final via the orchestrator's held-window release), errors, and cancellation all keep the existing exactly-once semantics — no new compatibility hacks.

## Decision 10 — Exactly-once normal finality

Normal completion SHALL result in exactly one final marker at each stage:

```text
last TextChunk.is_final   = True
last AudioWindow.is_final = True
last VideoWindow.is_final = True
```

Earlier windows are non-final. Empty trailing artifacts are not created only to carry finality.

If TTS does not accept `TextChunk.is_final` as an input contract, the orchestrator uses one-window lookahead over each TTS iterator and stamps finality on the last produced `AudioWindow` for the final text chunk. Video propagation preserves that marker.

Cancellation, upstream error, or TTS error SHALL terminate the utterance through the existing error/cancel path and SHALL NOT synthesize a successful final marker.

## Decision 11 — Telemetry closes the VieNeu control loop

Per emitted chunk, record structured, content-free-by-default telemetry:

- `chunk_seq`;
- `decision_reason` (`sentence`, `clause`, `target`, `latency_deadline`, `hard_max`, `finalize`, etc.);
- character length;
- estimated speech duration;
- TTS first-audio/synthesis latency when available;
- actual generated audio duration;
- TTS RTF;
- playback buffer before/after enqueue when available;
- whether a protected-span fallback or hard split occurred.

Do not log script/chunk text by default. Existing observability redaction rules remain authoritative.

TTS telemetry is summarized with bounded EWMA state per active utterance/session; the chunker receives only `RuntimeHints`, not the telemetry collector itself.

## Decision 12 — Benchmark determines the adaptive operating point

The benchmark compares current fixed behavior against adaptive candidates on a fixed Vietnamese corpus containing at least:

- short and long conversational sentences;
- commas/clauses and multi-sentence paragraphs;
- prices, currency, percentages, grouped/decimal numbers;
- product names, SKUs, acronyms, mixed Vietnamese/English terms;
- complete-script input;
- the same text fragmented into realistic streaming delta patterns.

For each candidate policy, collect:

- time-to-first-audio (TTFA) p50/p95;
- first-chunk estimated and actual audio duration;
- TTS first-audio and total synthesis latency;
- TTS RTF;
- playback underrun/starvation count;
- chunk count and duration distribution;
- hard-split/protected-span violation rate;
- exact text-preservation/finality results.

Produce paired VieNeu audio for human review. The human gate evaluates natural pauses, continuity, awkward cuts, and overall naturalness against the fixed baseline.

### PASS rule

1. All correctness invariants must pass with zero text loss/reorder, zero max-cap violations, zero false successful-final markers, and zero protected-span splits where a safe candidate existed.
2. A candidate is **prosody-eligible** only if human review finds no material naturalness regression versus baseline.
3. Among prosody-eligible candidates, choose the one with the lowest median TTFA, provided TTFA p95 does not regress by more than 5% versus baseline.
4. If no adaptive candidate satisfies correctness + prosody + TTFA constraints, Change A remains NOT PASS and Change B stays blocked. The fixed policy remains the runtime default.
5. Benchmark evidence and the selected calibrated constants/weights are committed with the change before marking the benchmark task complete.

This rule operationalizes “start speaking as fast as possible while preserving VieNeu prosody” as a constrained optimization rather than an arbitrary fixed threshold.

## Failure handling

- Empty fragments are ignored and do not start buffer age.
- Chunker analysis errors fail closed to deterministic fixed-policy segmentation for the current utterance and emit an error metric; they do not drop text.
- Streaming producer exceptions are propagated to the consumer and terminate the utterance through the existing error path.
- Queue full applies bounded backpressure; text is never silently dropped.
- Cancellation clears buffered state and does not emit a normal final chunk.

## Downstream dependency

`approved-script-authoring-pipeline` is explicitly blocked on this change reaching VieNeu benchmark PASS. That downstream change owns script drafting, LLM-assisted authoring, formatting/content gates, moderation, human approval, immutable approved-script identity, and delivery of approved `spoken_text` into this chunker. None of those responsibilities may be pulled into this change during implementation.

## Rollout

1. Land correctness invariants and canonical types first while keeping fixed behavior available.
2. Land deterministic Vietnamese boundary/duration modules behind the adaptive policy switch.
3. Land real streaming deadline and finality normalization.
4. Add telemetry and adaptive runtime hints.
5. Run fixed-vs-adaptive VieNeu benchmark and calibrate policy.
6. Only after benchmark PASS make `adaptive_vi` the intended default and authorize Change B planning.

## Risks and mitigations

### Risk: heuristic complexity makes behavior hard to debug

Mitigation: every split emits a deterministic `decision_reason`, candidate scoring is pure/testable, and fixed policy stays available as rollback.

### Risk: producer thread leaks on blocked upstream I/O

Mitigation: bounded one-producer-per-utterance lifecycle, explicit stop/close, finite provider I/O timeouts, and tests that assert producer cleanup after EOF/error/cancel.

### Risk: optimizing TTFA degrades prosody

Mitigation: adaptive policy is benchmark-gated by human VieNeu prosody non-regression; no latency result alone can pass the change.

### Risk: full-script and streaming behavior drift

Mitigation: source identity is absent from the chunker contract and fragmentation-invariance tests run the same text under multiple `feed()` fragmentations; only explicit realtime deadline flushes may differ.

### Risk: duration estimator is inaccurate before calibration

Mitigation: estimator is a soft score, hard correctness is character/span based, `target_chars` remains a fallback, and VieNeu actual-audio telemetry calibrates coefficients before adaptive default promotion.
