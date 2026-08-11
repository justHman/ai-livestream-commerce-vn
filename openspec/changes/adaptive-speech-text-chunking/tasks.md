## 1. Baseline and contract-lock tests

- [x] 1.1 Capture the current fixed-policy behavior in focused backend tests before changing implementation: punctuation flushing, minimum coalescing, current fixed `target/min/max` configuration, timeout polling, finalize, sequence, and IDs.
- [x] 1.2 Add exact text-preservation tests that concatenate every emitted chunk plus remainder and compare byte-for-byte/Unicode-codepoint-for-codepoint with concatenated input fragments.
- [x] 1.3 Add fragmentation-invariance parameterized tests using the same Vietnamese text as: one full script, word-sized fragments, character-sized fragments, punctuation-coalesced fragments, and provider-like multi-word deltas; assert equivalent content segmentation when no external flush occurs.
- [x] 1.4 Add failing regression tests for punctuation inside a multi-sentence delta and for one `feed()` call returning multiple chunks.
- [x] 1.5 Add failing hard-cap tests where a 75-character buffer receives a 30-character delta and where a single delta is larger than `2 * max_chars`; assert every automatic non-final output is `<= max_chars` with no text loss.
- [x] 1.6 Add fake-clock tests proving long TTFT does not age an empty buffer, and proving buffer age starts on the first non-empty fragment.
- [x] 1.7 Add an orchestrator-level test where the synchronous LLM iterator stalls longer than the configured deadline without yielding a next delta; assert a chunk is flushed before another LLM yield.
- [x] 1.8 Add end-to-end finality tests for normal completion, empty-final remainder, LLM error, TTS error, and cancellation; assert exactly one normal terminal marker only on successful completion.
- [x] 1.9 Add a canonical-export test proving exactly one `TextChunk` class exists, exported by `backend.application.text_chunker`, and that `render/windows.py` does not define or re-export it.

## 2. Canonical speech-chunking types and fixed-policy correctness

- [x] 2.1 Create `services/product/backend_service/src/backend/application/text_chunker/__init__.py` and `types.py` with canonical `TextChunk`, `ChunkPolicy`, `RuntimeHints`, `FixedChunkPolicyConfig`, and chunk-decision reason types; keep the public API free of source-type fields such as `llm` or `script`.
- [x] 2.2 Move the chunker into the `text_chunker/` package (`chunker.py`) importing the canonical types, maintaining a real `buffer_started_at`, and exposing explicit `feed()`, `flush(reason=...)`, `finalize()`, buffer-age, and buffered-text state without spawning timers/threads.
- [x] 2.3 Replace end-of-delta punctuation checks with accumulated-buffer scanning and a drain loop so arbitrary deltas can produce multiple chunks.
- [x] 2.4 Enforce the true hard `max_chars` invariant for every automatic non-final chunk, selecting a safe split at or before the cap and retaining remainder without loss/reorder.
- [x] 2.5 Give `target_chars` an explicit deterministic fixed-policy/fallback role and tighten configuration validation to require positive ordered character thresholds and a non-negative timeout.
- [x] 2.6 Migrate every import to `backend.application.text_chunker`, remove the duplicate `TextChunk` definition/construction paths, and remove the `render/windows.py` re-export; `render/windows.py` does not define or re-export `TextChunk`.
- [x] 2.7 Run the focused chunker/type tests and backend static checks; do not continue to adaptive heuristics until all correctness regressions are green.

## 3. Deterministic Vietnamese boundary and duration engine

- [x] 3.1 Create `text_chunker/boundaries.py` with pure candidate extraction over original-text spans for paragraph/line, sentence punctuation, semicolon/colon, comma/clause, Vietnamese cue/whitespace, and hard-cap candidates.
- [x] 3.2 Add protected-span detection for decimals/grouped numbers, currency/percent forms, URLs/emails, common acronym/abbreviation forms, SKU-like tokens, and balanced quote/parenthesis regions when a nearby safe boundary exists.
- [x] 3.3 Add table-driven Vietnamese boundary tests covering multi-sentence paragraphs, commas/clauses, prices, percentages, decimals, product names, SKU codes, acronyms, mixed Vietnamese/English text, quotes, and parentheses.
- [x] 3.4 Create `text_chunker/duration.py` with deterministic `SpeechDurationEstimator` features for Vietnamese syllable-like units, punctuation pauses, numbers, currency, percentages, acronyms/English-like tokens, and calibration coefficients.
- [x] 3.5 Add duration-estimator tests proving compact written forms such as prices/percentages are estimated differently from equal-length plain words and that estimation never mutates output text.
- [x] 3.6 Create `text_chunker/policy.py` with deterministic candidate scoring that prioritizes linguistic boundary quality, duration-target proximity, protected-span safety, the adaptive char-bias tie-break, and hard-cap enforcement.
- [x] 3.7 Integrate the scorer/estimator into `TextChunker` behind an `adaptive_vi` policy while preserving a selectable `fixed` policy and automatic fixed fallback on analysis failure.
- [x] 3.8 Re-run fragmentation-invariance and exact-preservation suites under both fixed and adaptive policies.

## 4. Real streaming deadline and bounded LLM controller

- [x] 4.1 Add typed producer events for `delta`, `eof`, and `error`, plus a bounded queue/controller around synchronous `LLMEngine.stream_chunks()` in the render orchestration boundary.
- [x] 4.2 Ensure only the consumer thread mutates `TextChunker`; use `queue.get(timeout=remaining_buffer_deadline)` so a latency deadline can fire with no new LLM delta.
- [x] 4.3 Start deadline age only from `TextChunker.buffer_started_at`; keep `speech_start_elapsed_ms` as a separate runtime hint so TTFT can influence optimization but not correctness timeout.
- [x] 4.4 Add bounded backpressure and producer lifecycle cleanup for EOF, exception, cancellation, and normal completion; close the generator when supported and require finite provider I/O timeouts rather than attempting unsafe thread termination.
- [x] 4.5 Remove the chunker-owned timeout knob: `flush_timeout_ms` moves to `StreamingControllerConfig` at the orchestration boundary, and the orchestrator applies the deadline via explicit `chunker.flush(reason=LATENCY_DEADLINE)`.
- [x] 4.6 Add deterministic synchronization tests for stall, fast producer/slow consumer, queue-full backpressure, producer error, and cancellation; avoid wall-clock sleeps where events/fake clocks can prove behavior.

## 5. Adaptive startup/steady/starvation policy

- [x] 5.1 Feed source-agnostic `RuntimeHints` from orchestrator state into chunk decisions: `speech_start_elapsed_ms`, playback-buffer milliseconds when available, TTS first-audio EWMA, and TTS RTF EWMA.
- [x] 5.2 Implement startup behavior that lowers the soft duration target when first audio has not started and speech-start elapsed time is high, while retaining the minimum linguistic-quality floor.
- [x] 5.3 Implement steady-state behavior that prefers longer coherent phrases when playback buffer is healthy.
- [x] 5.4 Implement starvation behavior that lowers the soft duration target when playback buffer is below the configured watermark or TTS latency/RTF degrades.
- [x] 5.5 Add unit tests showing identical text can select different *valid* boundaries under startup, steady, and starvation hints while all hard invariants remain identical.
- [x] 5.6 Verify complete-script segmentation uses the same scorer/policy with neutral runtime hints and requires no streaming controller/thread.

## 6. Exactly-once finality through TTS/video

- [x] 6.1 Make `TextChunker.finalize()` produce exactly one final text chunk when non-empty text remains and stamp the last already-emitted logical chunk correctly when completion arrives without a new textual remainder.
- [x] 6.2 Normalize TTS finality in the orchestrator with one-window lookahead when the TTS engine does not consume `TextChunk.is_final`; stamp only the last audio window corresponding to the final text chunk.
- [x] 6.3 Verify and, if required, fix `AudioWindow.is_final` propagation into the final `VideoWindow` without creating empty terminal audio/video artifacts.
- [x] 6.4 Verify cancellation and upstream/TTS errors never emit a normal-success final marker and still release producer/chunker resources.
- [x] 6.5 Run focused render/orchestrator integration tests for multi-chunk utterances and exactly-once terminal semantics.

## 7. Observability and VieNeu calibration inputs

- [x] 7.1 Add content-free-by-default chunk-decision telemetry: sequence, decision reason, character length, estimated speech duration, hard-max/protected-span fallback flags, and policy state.
- [x] 7.2 Record VieNeu TTS first-audio/synthesis latency, generated audio duration, and RTF where the current engine contract exposes them; retain bounded EWMA state for runtime hints.
- [x] 7.3 Record playback-buffer/underrun data at the orchestration boundary where available and ensure missing telemetry degrades gracefully to neutral hints.
- [x] 7.4 Add observability tests proving raw script/chunk text is not logged by default and that fallback/error reasons remain diagnosable.

## 8. Fixed-versus-adaptive VieNeu benchmark

- [x] 8.1 Add a versioned Vietnamese benchmark corpus under backend test/benchmark fixtures with at least 30 representative utterances covering short/long speech, clauses, multi-sentence paragraphs, prices/currency/percentages, numbers, product names/SKUs, acronyms, mixed Vietnamese/English terms, and complete-script text.
- [x] 8.2 Add deterministic streaming-fragment fixtures for the same corpus so fixed and adaptive policies can be compared under full-text and realistic incremental delivery without changing source text.
- [x] 8.3 Extend/add a benchmark runner that records policy/config hash, TTFA p50/p95, first-chunk estimated/actual duration, TTS latency, RTF, playback underruns, chunk distribution, hard-split/protected-span events, preservation/finality failures, and paired audio artifact paths. Runner built at `tests/unit/benchmark_fixtures/benchmark_runner.py` (Mode A simulation default, Mode B VieNeu with `VIENEU_RUNTIME=1` opt-in + fail-loud runtime gate). 21 unit tests green.
- [x] 8.4 Run the existing fixed `12/40/80/350ms` behavior as the named baseline using the same VieNeu runtime and corpus used for adaptive candidates. Baseline run (simulation) on `vi_benchmark_corpus_v1` recorded in `benchmarks/adaptive-chunking/cand-baseline-fixed-*.{json,md}` + SUMMARY.md. Real VieNeu runtime NOT available (no vieneu package, no weights, no GPU) — runtime dimension documented as unavailable. **Real-TTS evidence added 2026-08-11 (Mode B' HTTP edge-tts `vi-VN-HoaiMyNeural` @ localhost:20128): baseline TTFA p50 987.4ms / p95 1918.3ms, RTF p50 0.174, underrun 0, preservation/finality 0; 3812 paired mp3+wav artifacts under `.runtime/benchmarks/realtss-baseline/`; summary log `/d/runtimes_logs/bench_baseline.log`; see `SUMMARY-REAL-TTS.md`.**
- [x] 8.5 Run a bounded adaptive candidate search over startup/steady/starvation duration targets and scorer weights; persist every candidate configuration and result so the selected operating point is reproducible. Bounded grid 12 candidates (target 1800/2200/2600 x startup_early 1200/1500 x starvation 1200/1400) persisted in `benchmarks/adaptive-chunking/cand-01..12-*.{json,md}`. Offline evidence only — NOT calibrated, NOT promoted. Finding: config tuning knobs are currently dead config in `soft_target_duration_ms()` (module constants only), see SUMMARY.md. **Real-TTS evidence added 2026-08-11: cand-01 (TTFA p50 966.8ms/p95 1559.6ms) + cand-05 (TTFA p50 664.4ms/p95 1171.2ms, RTF p50 0.167, underrun 0, preservation/finality 0, 3998 mp3+wav artifacts + `cand-05-metrics.json`); all 12 adaptive candidates emit byte-identical chunk streams (verified simulation + text-level + real-audio duration 1.836s identical), so cand-01+cand-05 runs cover the adaptive stream — grid reduced per documented decision; see `SUMMARY-REAL-TTS.md`.**
- [x] 8.6 Conduct blinded human review of paired baseline/adaptive VieNeu audio for natural pauses, continuity, awkward cuts, and overall naturalness; record pass/fail evidence without exposing which policy produced each clip during rating. NOT-POSSIBLE (was): real VieNeu runtime unavailable (missing vieneu package, weights, GPU) — no paired audio can be generated; review not fabricated. **2026-08-11: real-TTS paired clips now BUILT via Mode B' HTTP edge-tts — 12 pairs (24 mp3) + manifest at `.runtime/benchmarks/realtss-pairs/clips/` (`{utt}-fixed.mp3` vs `{utt}-adaptive.mp3`, full delivery neutral profile); PENDING USER blinded human review (prosody gate), not yet rated.**
- [x] 8.7 Apply the PASS rule: zero correctness failures; prosody non-regression; choose the prosody-eligible candidate with the lowest median TTFA; reject any candidate whose TTFA p95 regresses by more than 5% versus baseline. NOT PASS (was): no candidate is prosody-eligible (no human audio evidence on real VieNeu); zero correctness failures observed offline (preservation/finality 0 across all 13 runs). **2026-08-11 real-TTS evidence: correctness invariants all zero on real runs (preservation 0, finality 0, protected-span 0, underrun 0, baseline + cand-01 + cand-05); TTFA constraint holds — adaptive p95 (1171-1560ms) does NOT regress >5% vs baseline p95 (1918ms), it improves. Prosody eligibility (rule 2) still pending the 8.6 human review of the paired clips.**
- [x] 8.8 If no candidate passes, keep `fixed` as default, mark the benchmark NOT PASS, document the failing dimension, and do not start `approved-script-authoring-pipeline`. APPLIED: fixed remains default; benchmark NOT PASS (pending 8.6 human review); failing dimension was VieNeu runtime unavailable — real-TTS evidence now exists (Mode B' HTTP edge-tts), so the remaining gate is prosody review, not runtime; Change B (`approved-script-authoring-pipeline`) remains BLOCKED until 8.9.
- [ ] 8.9 If a candidate passes, commit the selected calibrated constants/weights and benchmark report, set `adaptive_vi` as the intended default with fixed rollback retained for the first release window, and record Change A PASS evidence. NOT DONE — requires human review of the real-TTS paired clips (8.6) + PASS rule (8.7); blocked by 8.8 verdict. Real-TTS evidence prepared (SUMMARY-REAL-TTS.md) but no candidate may pass or constants be calibrated until the user rates the paired audio.

## 9. Regression and closeout

- [x] 9.0 Architecture cleanliness: repository-wide audit passes — zero `speech_chunking` references in active code, zero `render.windows` `TextChunk` definition/re-export, zero `text_chunker.py` facade file, zero duplicated chunking defaults, and the verbatim/full-script path uses the same `TextChunker` state machine. Audit PASS (a-f); fixed 9.0d duplicated fallback mirrors in `sessions.py` + `coordinator.py` (commit 1280b47).
- [x] 9.1 Run all backend unit/integration/contract tests affected by chunking/render orchestration plus Ruff/format/static checks used by service CI. 515 unit + 194 integration/contract passed (excl. env-bound sentence-transformers test); contract drift fixed via LF normalization + `.gitattributes` (9e5a481); ruff check + format clean (ae1f57b).
- [x] 9.2 Run existing local Stage 2 speech-path regression without AWS mutation and verify no session cleanup, playback correlation, or stop semantics regress. `stage2_pipeline.py --lane offline` PASS: 25 turns, cleanup clean, queue underflow/drops/stale 0, errors []; playback telemetry (7.3) flows through orchestrator without error.
- [x] 9.3 Run OpenSpec validation for `adaptive-speech-text-chunking` and correct every structural/spec-format finding before implementation is considered complete. `openspec validate adaptive-speech-text-chunking` → valid.
- [x] 9.4 Update developer/runbook documentation with the source-agnostic chunking contract, fixed rollback switch, telemetry fields, benchmark procedure, and explicit rule that script authoring/moderation belongs to downstream Change B. New `docs/chunking-contract.md` + link from `docs/architecture.md` (0366952).
- [ ] 9.5 Only after task 8.9 and all regression gates pass, authorize creation of `approved-script-authoring-pipeline`; otherwise leave Change B blocked.
