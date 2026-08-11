# Chunking contract — adaptive speech-text chunking (Change A)

> Source-agnostic text segmentation for the streaming speech pipeline.
> Canonical code lives in
> `services/product/backend_service/src/backend/application/text_chunker/`.

## Scope and ownership

- The **text chunker** segments arbitrary text (LLM deltas, full scripts,
  verbatim speech) into spoken-phrase chunks. It is content segmentation
  **only** — no timers, no threads, no TTS knowledge.
- The **streaming controller** owns the realtime buffer deadline
  (`StreamingControllerConfig.flush_timeout_ms`) and applies it via
  `chunker.flush(reason=LATENCY_DEADLINE)`.
- Script authoring, formatting, moderation, and human approval belong to
  downstream **Change B** (`approved-script-authoring-pipeline`) and must
  not be pulled into this contract.

## Invariants

| Invariant | Guarantee |
|---|---|
| Text preservation | Concatenating all emitted chunks plus final remainder equals input byte-for-byte / codepoint-for-codepoint |
| Fragmentation invariance | Identical text + identical policy/hints + no external flush ⇒ identical segmentation regardless of `feed()` fragment sizes |
| Hard `max_chars` | Every automatic non-final chunk ≤ `max_chars` (forced split at best safe position; `decision_reason=hard_max`) |
| Exactly-once finality | `finalize()` emits exactly one final chunk when non-empty text remains; one normal terminal marker only on successful completion |
| Protected spans | Decimals, grouped numbers, currency/percent, URLs/emails, acronyms, SKU forms, balanced quotes/parens never split when a safe candidate exists |

## Config

Canonical config types (single source of truth, no duplicated defaults):

```text
FixedChunkPolicyConfig   min_chars=12 target_chars=40 max_chars=80   (rollback baseline)
AdaptiveViPolicyConfig   startup/steady/starvation duration targets + scorer weights
StreamingControllerConfig flush_timeout_ms=350                         (buffer deadline)
```

Runtime env knobs (see `backend/config.py`):

```text
TEXT_CHUNK_MIN_CHARS / TEXT_CHUNK_TARGET_CHARS / TEXT_CHUNK_MAX_CHARS
TEXT_CHUNK_FLUSH_TIMEOUT_MS
```

Chunking policy is selected by `ChunkPolicy` (`fixed` | `adaptive_vi`).

## Fixed rollback switch

- Default remains **`fixed`** until the VieNeu benchmark gate passes
  (task 8.9). `adaptive_vi` is never the silent default.
- Rollback: switch `ChunkPolicy` to `fixed` — identical behavior to the
  pre-change baseline (12/40/80/350ms), retained for the first release
  window.

## Telemetry fields (content-free)

| Field | Meaning |
|---|---|
| `seq`, `decision_reason` | chunk sequence + why this boundary was chosen |
| `char_len`, `estimated_speech_duration_ms` | content-length only, never text |
| `hard_max_used`, `protected_span_fallback` | fallback/forced-split flags |
| `policy_state` | current fixed/adaptive operating point |

Runtime hints (source-agnostic, fed into adaptive scoring):

```text
speech_start_elapsed_ms   TTFT influence on optimization, never on correctness timeout
playback_buffer_ms        healthy buffer ⇒ prefer longer phrases (EWMA, NaN-safe)
tts_first_audio_ewma_ms / tts_rtf_ewma   bounded EWMA state
```

Raw script/chunk text is never logged by default.

## Benchmark procedure

1. Runner: `services/product/backend_service/tests/unit/benchmark_fixtures/benchmark_runner.py`
   — Mode A (deterministic simulation) default; Mode B (real VieNeu) gated
   behind `VIENEU_RUNTIME=1` + package/weights presence, fails loud.
2. Corpus: `tests/unit/benchmark_fixtures/vi_benchmark_corpus_v1.json`
   (40 utterances × 10 categories × 4 delivery forms).
3. Metrics: TTFA p50/p95, first-chunk duration, TTS latency, RTF, playback
   underruns, chunk distribution, hard-split/protected-span events,
   preservation/finality failures, paired audio paths.
4. Evidence: `benchmarks/adaptive-chunking/` (per-candidate JSON+MD +
   SUMMARY.md with verdicts, runtime availability, reproducibility metadata).
5. PASS rule (Decision 12): zero correctness failures + human VieNeu prosody
   non-regression + lowest median TTFA among prosody-eligible candidates with
   TTFA p95 ≤5% vs baseline. No candidate passes on simulation alone.
6. 2026-08-11 verdict: **NOT PASS** (VieNeu runtime unavailable local) —
   `fixed` remains default; Change B remains **BLOCKED**.

## Change B boundary

- `approved-script-authoring-pipeline` owns script drafting, LLM-assisted
  authoring, formatting/content gates, moderation, human approval, immutable
  approved-script identity, and delivering approved `spoken_text` into this
  chunker.
- It must not start until task 8.9 records PASS (real VieNeu benchmark +
  human review). Currently blocked.
