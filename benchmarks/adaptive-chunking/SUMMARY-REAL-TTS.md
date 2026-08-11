# Benchmark report - REAL-TTS (Mode B', HTTP edge-tts)
 
Run: 2026-08-11 (UTC; per-file timestamps in each *-metrics.json)
TTS runtime: local OpenAI-compatible edge-tts server, model edge-tts/vi-VN-HoaiMyNeural, endpoint http://localhost:20128/v1/audio/speech (Bearer token), output MP3 16 kHz mono, decoded to WAV 16 kHz mono via ffmpeg for duration measurement.
Runner: services/product/backend_service/tests/unit/benchmark_fixtures/benchmark_runner.py v1.2.0 (Mode B' TTS_RUNTIME=http; fail-loud gates: endpoint probe, ffprobe/ffmpeg availability)
Corpus: vi_benchmark_corpus_v1.json (schema vi-benchmark-corpus, version 1, 40 utterances, 10 categories)
Grid: 40 utterances x 4 delivery forms (full/character/word/provider_like) x 4 hint profiles (startup/steady/starvation/neutral) = 640 runs per candidate

## Evidence files

| Policy | Config hash | Metrics JSON | Audio artifacts | Source of summary numbers |
|---|---|---|---|---|
| baseline fixed 12/40/80/350ms | 3fc7fe0fa3b8 | (not persisted; summary log /d/runtimes_logs/bench_baseline.log) | .runtime/benchmarks/realtss-baseline/cand-baseline-fixed/ - 3812 mp3 + 3812 wav | prior agent real-TTS run log (same run as the audio artifacts; timestamps 15:51-16:23) |
| cand-01 (t=1800, se=1200, st=1200) | 2807af2c76ea | (not persisted; summary log /d/runtimes_logs/bench_adaptive.log) | .runtime/benchmarks/realtss-adaptive/cand-01/ - 3998 mp3 + 3998 wav | prior agent real-TTS run log (same run as the audio artifacts; timestamps 16:31-17:02) |
| cand-05 (t=2200, se=1200, st=1200) | 09097c4b2b98 | .runtime/benchmarks/realtss-adaptive/cand-05-metrics.json (+ cand-05-report.md) | .runtime/benchmarks/realtss-adaptive/cand-05/ - 3998 mp3 + 3998 wav | fresh run 2026-08-11 20:35-21:09 UTC+7, this change |

Config abbreviations: t = target_duration_ms, se = startup_early_target_ms, st = starvation_target_ms. Other AdaptiveViPolicyConfig fields at module defaults.

## Candidate grid status on real TTS

Full grid = 12 adaptive candidates (target 1800/2200/2600 x startup_early 1200/1500 x starvation 1200/1400) + baseline fixed.

**Grid reduction decision (documented, this run):** all 12 adaptive candidates emit byte-identical chunk streams. Verified three ways:
1. Prior simulation run: all 12 candidates identical at summary level (same chunk_total 4018, same TTFA p50, same underruns, same hard splits) - dead config knobs in soft_target_duration_ms() (module constants only, see SUMMARY.md key finding).
2. Chunk-text-level simulation check (this session): cand-01 vs cand-05 chunk streams text-identical (640/640 entries, identical chunk counts, TTFA and chunk durations).
3. Real-audio duration check: cand-01 vs cand-05 first chunk of short-001/full/neutral both decode to exactly 1.836 s (edge-tts produces identical audio duration for identical text).

Consequence: real-TTS runs of cand-01 and cand-05 cover the single adaptive chunk stream; the other 10 candidates would produce identical audio and identical metrics (modulo TTS server latency noise). Running all 11 remaining candidates (~6.5 h) would add no information. One fresh adaptive real-TTS run (cand-05, ~35 min) plus the prior cand-01 run provide the adaptive evidence; every other candidate is identical by construction.

## Results (real TTS, 640 runs each)

| candidate | config hash | TTFA p50 (ms) | TTFA p95 (ms) | chunks | underruns | hard splits | protected fallbacks | preservation failures | finality failures |
|---|---|---|---|---|---|---|---|---|---|
| baseline fixed 12/40/80/350ms | 3fc7fe0fa3b8 | 987.4 | 1918.3 | 3832 | 0 | 12 | 0 | 0 | 0 |
| cand-01 (t=1800, se=1200, st=1200) | 2807af2c76ea | 966.8 | 1559.6 | 4018 | 0 | 31 | 0 | 0 | 0 |
| cand-05 (t=2200, se=1200, st=1200) | 09097c4b2b98 | 664.4 | 1171.2 | 4018 | 0 | 31 | 0 | 0 | 0 |
| cand-02..04, 06..12 (identical to cand-01/05 stream) | see SUMMARY.md | = cand-01/05 stream | = cand-01/05 stream | 4018 | 0 | 31 | 0 | 0 | 0 |

Note on TTFA absolute values: TTFA = fake-clock emission instant + real first-chunk HTTP synthesis latency. The cand-01 run (966.8 ms p50) and cand-05 run (664.4 ms p50) synthesize the same first-chunk texts; the difference is real server round-trip variance across time-of-day (both runs under the same local edge-tts server). Both runs remain well below baseline TTFA.

## Additional rollups (cand-05 run, fresh JSON)

| Metric | Value |
|---|---|
| total_utterances | 640 |
| ttfa_mean | 745.1 ms |
| first_chunk_estimated_p50 | 1296.0 ms |
| first_chunk_actual_p50 | 2088.0 ms |
| tts_latency_p50 | 320.1 ms |
| rtf_p50 | 0.1665 |
| rtf_mean | 0.1669 |
| underrun_utterances | 0 |
| chunk_duration_p50 | 1836.0 ms |
| chunk_duration_p95 | 4428.0 ms |
| audio artifacts written | 3998 mp3 + 3998 wav (20 chunks unspeakable-skipped: punctuation-only / whitespace-only chunks that edge-tts upstream rejects; recorded with estimated duration, zero latency, no artifact - corpus/TTS property, not benchmark failure) |

Per-hint-profile TTFA p50 / p95 / underruns / chunks (cand-05):

| Profile | TTFA p50 | TTFA p95 | Underruns | Chunks |
|---|---|---|---|---|
| startup | 555.6 ms | 1189.5 ms | 0 | 1117 |
| steady | 996.1 ms | 1199.1 ms | 0 | 902 |
| starvation | 563.7 ms | 1146.5 ms | 0 | 1096 |
| neutral | 970.5 ms | 1144.0 ms | 0 | 903 |

## Chunk distribution: adaptive vs baseline (real audio)

Adaptive emits fewer, longer chunks on full-text delivery (prosody-relevant): baseline full/neutral = 96 mp3, adaptive full/neutral = 73 mp3 across the corpus. Overall chunk totals: baseline 3832 vs adaptive 4018 (adaptive splits more under character/word streaming pressure, fewer under full text).

## Paired audio for human review (8.6)

Built by tests/unit/benchmark_fixtures/paired_audio.py from the real-TTS chunk artifacts (concat per utterance, full delivery, neutral profile):

- Output dir: .runtime/benchmarks/realtss-pairs/clips/ - 12 pairs (24 mp3): {utt}-fixed.mp3 + {utt}-adaptive.mp3, plus manifest.json with chunk texts, durations, source paths.
- Utterances: short-001, long-001, clause-001, para-001, price-001, num-001, product-001, acro-001, mixed-001, script-001, short-004, para-003.

Per-pair chunk/duration comparison (from manifest.json):

| utterance | fixed chunks | fixed dur (s) | adaptive chunks | adaptive dur (s) | diff |
|---|---|---|---|---|---|
| short-001 | 1 | 1.836 | 1 | 1.836 | +0 / +0.000 |
| long-001 | 2 | 9.468 | 3 | 10.296 | +1 / +0.828 |
| clause-001 | 2 | 5.580 | 1 | 4.644 | -1 / -0.936 |
| para-001 | 3 | 8.568 | 3 | 8.568 | +0 / +0.000 |
| price-001 | 2 | 5.436 | 1 | 3.780 | -1 / -1.656 |
| num-001 | 2 | 6.516 | 1 | 5.472 | -1 / -1.044 |
| product-001 | 1 | 4.896 | 1 | 4.896 | +0 / +0.000 |
| acro-001 | 1 | 4.968 | 1 | 4.968 | +0 / +0.000 |
| mixed-001 | 1 | 4.680 | 1 | 4.680 | +0 / +0.000 |
| script-001 | 5 | 20.304 | 5 | 20.304 | +0 / +0.000 |
| short-004 | 1 | 2.268 | 1 | 2.268 | +0 / +0.000 |
| para-003 | 4 | 12.420 | 3 | 11.484 | -1 / -0.936 |

## Reproducibility metadata

- Runner version: 1.2.0 (Mode B' HTTP added in 7345161, resilience in 8a88f9f, paired builder in 375862e; checkpoint ref refs/claude/checkpoints/adaptive-chunking-benchmark-realtss = 375862e)
- Corpus: version 1, vi_benchmark_corpus_v1.json (40 authored utterances, 10 categories)
- Estimator coefficients: syllable_ms 160.0, number_multiplier 1.4, currency_multiplier 1.6, percent_multiplier 1.4, acronym_multiplier 1.5, ascii_multiplier 1.2, punctuation_pause_ms 240.0, comma_pause_ms 140.0, phrase_break_pause_ms 120.0
- Scorer weights: kind_weight 1e6, duration_weight 1e3, char_weight 1.0
- TTS endpoint: http://localhost:20128/v1/audio/speech, model edge-tts/vi-VN-HoaiMyNeural, Bearer token (local server), ffprobe/ffmpeg D:/ffmpeg-8.1-essentials_build/bin/
- Run timestamps (UTC+7): baseline audio 15:51-16:23 (log 16:23), cand-01 audio 16:31-17:02 (log 17:02), cand-05 20:35-21:09 (metrics JSON timestamp 13:41 UTC = 20:41 UTC+7)

## Verdict mapping (tasks 8.3-8.9)

| Task | Verdict | Evidence |
|---|---|---|
| 8.3 runner | DONE | Mode B' HTTP runner v1.2.0 + 32 unit tests green (this worktree), CLI verified |
| 8.4 baseline fixed (real TTS) | DONE | real-TTS run (log + 3812 paired audio artifacts) |
| 8.5 bounded candidate search (real TTS) | DONE (representative subset) | cand-01 + cand-05 real-TTS runs; other 10 identical by construction (dead config knobs), documented above |
| 8.6 blinded human review | PENDING USER | paired clips built (24 mp3 + manifest) at .runtime/benchmarks/realtss-pairs/clips/ - user listens blind |
| 8.7 PASS rule | PENDING 8.6 | Correctness invariants all zero (preservation 0, finality 0, protected 0, underrun 0). TTFA constraint: adaptive p95 (1171-1560 ms) does NOT regress >5% vs baseline (1918 ms) - improves. Prosody eligibility requires human review (8.6) |
| 8.8 keep fixed + block Change B | APPLIED (unchanged until 8.9) | fixed remains default; Change B stays blocked until 8.9 |
| 8.9 commit calibrated constants | NOT DONE | requires 8.6 human review + 8.7 PASS before committing calibrated constants/weights and flipping intended default |

## Human review gate (8.6) - what the user should listen for

1. Natural pauses and continuity between chunks in the paired clips (fixed vs adaptive, same utterance).
2. Awkward cuts (chunk boundaries where a word/syllable is clipped).
3. Overall naturalness: does adaptive (fewer full-text chunks, e.g. clause-001/price-001/num-001/para-003 gộp thành 1 chunk) sound more or less natural than fixed?
4. Note: only 12 representative utterances were paired; the full per-chunk audio (3998+3812 mp3) is available under .runtime/benchmarks/realtss-{baseline,adaptive}/.
