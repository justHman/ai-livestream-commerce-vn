## Why

The current backend text chunker was built around incremental LLM output and fixed `min/target/max` character thresholds plus a nominal flush timeout. That implementation is simple, but the reviewed behavior has correctness and latency gaps: the timeout cannot fire while the synchronous LLM stream is idle, buffer age includes pre-first-token time, `target_chars` is not a real decision input, a multi-character delta can overshoot `max_chars`, punctuation inside a delta is missed, finality propagation is not a single explicit end-to-end contract, and two distinct `TextChunk` dataclasses exist.

The product direction also requires the same speech segmentation primitive to accept both realtime LLM fragments and complete prewritten scripts. Maintaining separate streaming and script chunkers would duplicate boundary logic and create drift. The chunker should therefore become source-agnostic: it segments arbitrary text fragments into natural speech chunks, while realtime waiting/deadline behavior stays in orchestration rather than being embedded as an LLM-specific mode.

For Vietnamese speech, character count is only a rough proxy for TTS quality and latency. The first spoken chunk should be short enough to minimize time-to-first-audio (TTFA), while steady-state chunks should be long and linguistically coherent enough for VieNeu to preserve natural prosody. The system therefore needs deterministic Vietnamese phrase-boundary scoring, estimated speech duration, runtime TTS/playback hints, and a measured VieNeu benchmark gate instead of relying only on `12/40/80/350ms`.

## What Changes

- Replace fixed-threshold-only segmentation with one **source-agnostic adaptive speech text chunker** that accepts arbitrary text fragments, including:
  - single-character or coalesced LLM streaming deltas;
  - sentences and paragraphs;
  - an entire prewritten script in one `feed()` call.
- Preserve current character thresholds as compatibility/safety signals, not as the primary adaptive decision mechanism:
  - `min_chars` remains a quality floor for normal automatic emission;
  - `target_chars` becomes a fallback/tie-break signal instead of dead configuration;
  - `max_chars` becomes a true hard safety cap for every non-final emitted chunk;
  - `flush_timeout_ms` becomes a real streaming-orchestration deadline and is not counted from LLM TTFT.
- Add deterministic **Vietnamese speech-boundary scoring** using punctuation, clause/phrase cues, protected spans, whitespace proximity, and estimated spoken duration. The first version adds no neural/NLP runtime dependency.
- Add a deterministic **speech-duration estimator** that accounts for Vietnamese syllable-like units, punctuation pauses, numbers, currency, percentages, acronyms, and other forms whose spoken duration differs materially from raw character count.
- Add **adaptive runtime hints** so the same chunker can prefer a smaller natural first chunk when speech startup is late, longer natural chunks during healthy steady-state playback, and earlier boundaries when playback is near starvation.
- Keep time-based streaming concerns outside the content segmentation algorithm. A bounded streaming controller around synchronous `LLMEngine.stream_chunks()` SHALL allow a deadline to fire even when no new LLM delta arrives.
- Establish explicit end-to-end finality: normal completion produces exactly one terminal final marker through `TextChunk` → `AudioWindow` → `VideoWindow`; cancellation does not fabricate a normal final completion.
- Unify the duplicate `TextChunk` definitions into one canonical type exported by the `backend.application.text_chunker` package; `render/windows.py` does not define or re-export it.
- Add observability for chunk reason, estimated duration, actual TTS latency/audio duration, playback-buffer state, and forced-split rate so the adaptive policy can be benchmarked and tuned.
- Add a VieNeu benchmark harness and acceptance gate. The selected adaptive policy SHALL be the lowest-TTFA candidate among configurations that satisfy correctness and human prosody non-regression versus the existing fixed-threshold baseline.

## Capabilities

### New Capabilities

- `adaptive-speech-text-chunking`: Source-agnostic Vietnamese speech segmentation, realtime deadline orchestration, canonical chunk finality/type semantics, adaptive duration/latency policy, telemetry, and VieNeu benchmark acceptance.

### Modified Capabilities

- *(none — the current repository does not expose an established `openspec/specs/` capability for speech text chunking)*

## Dependency and Sequencing

This is **Change A**. It MUST reach VieNeu benchmark PASS before starting **Change B `approved-script-authoring-pipeline`**.

Change B will own script drafting, LLM-assisted authoring skill use, formatting/content gates, profanity/toxicity checks, commerce-claim validation, human review, immutable approval/version hashes, and the transition from approved `spoken_text` into this chunker. Those authoring/safety responsibilities are intentionally not implemented in Change A.

## Impact

- **Backend application**: the final capability is `backend.application.text_chunker` — one cohesive package (chunker state machine + types/boundaries/duration/policy/telemetry modules) providing a source-agnostic segmentation state machine with selectable fixed/adaptive strategies.
- **Render orchestration**: synchronous LLM streaming is isolated behind a bounded timed controller; finality and runtime hints are normalized before TTS/video propagation.
- **Types**: one canonical `TextChunk` class, exported by `backend.application.text_chunker`; `render/windows.py` defines or re-exports nothing.
- **Configuration**: existing `TEXT_CHUNK_*` settings remain the env source of truth; callers build typed `FixedChunkPolicyConfig` / `StreamingControllerConfig` objects from `AppConfig` once, with no duplicated defaults.
- **Tests**: add arbitrary-fragment, fragmentation-invariance, hard-cap, punctuation-inside-delta, true-timeout, finality, adaptive-policy, and benchmark-regression coverage.
- **Observability**: add chunk decision reason and latency/prosody-relevant metrics without logging script contents by default.
- **Runtime dependencies**: no new model, tokenizer service, embedding model, or network dependency is required for chunk decisions.
- **Out of scope**: script moderation/approval, AI-slop detection, spelling/profanity datasets, LLM script generation skills, SSML authoring, Vietnamese neural parsing, model retraining, and AWS deployment changes.
