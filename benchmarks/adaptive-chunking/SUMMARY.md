# Benchmark report — fixed-versus-adaptive VieNeu chunking (OpenSpec 8.3-8.9)

Run: 2026-08-11 (UTC; per-file timestamps in each *-metrics.json)
Runner: services/product/backend_service/tests/unit/benchmark_fixtures/benchmark_runner.py v1.1.0
Corpus: vi_benchmark_corpus_v1.json (schema vi-benchmark-corpus, version 1, 40 utterances, 10 categories)
Grid: 40 utterances x 4 delivery forms (full/character/word/provider_like) x 4 hint profiles (startup/steady/starvation/neutral) = 640 runs per candidate

## Runtime availability (Mode B gate)

| Component | Status | Evidence |
|---|---|---|
| vieneu package | NOT installed | probe_runtime() -> vieneu_package False (absent from backend_service/.venv and tts_service/.venv; optional extra vieneu = ["vieneu"] in tts_service pyproject) |
| VieNeu weights | NOT configured | probe_runtime() -> weights_source none (HF repo pnnbao-ump/VieNeu-TTS-v3-Turbo not downloaded; VN download throttled ~37KB/s per project memory hf-throttle-vn-use-gha-seed) |
| GPU | UNAVAILABLE | torch 2.13.0+cpu, torch.cuda.is_available() False (Intel UHD + RTX 3050 Laptop, no CUDA runtime) |
| VIENEU_RUNTIME=1 opt-in | not set | Mode B refuses to run without explicit opt-in (fail-loud) |

Conclusion: Mode B (real VieNeu runtime) cannot run in this environment. All metrics below are Mode A simulations (deterministic, no real TTS). Per Decision 12.4, no candidate can PASS on simulation evidence alone.

## Verdicts per task

| Task | Verdict | Evidence |
|---|---|---|
| 8.3 runner | DONE | Runner + 21 unit tests (test_benchmark_runner.py), 80 corpus tests, 16 adaptive tests green; CLI smoke OK |
| 8.4 baseline fixed | RUN (simulation) | cand-baseline-fixed-*.{json,md}; real VieNeu runtime NOT run (runtime unavailable) |
| 8.5 bounded candidate search | RUN (simulation) | cand-01..12-*.{json,md}; no random search; offline evidence only, NO promote |
| 8.6 blinded human review | NOT POSSIBLE | Requires paired real VieNeu audio (package + weights + GPU); runtime unavailable; review cannot be fabricated |
| 8.7 PASS rule | NOT PASS | No prosody-eligible candidate exists (no human audio); TTFA comparison moot offline |
| 8.8 keep fixed + block Change B | APPLIED | fixed remains default; approved-script-authoring-pipeline stays BLOCKED; failing dimension = VieNeu runtime unavailable |
| 8.9 commit calibrated constants | NOT DONE | Requires real runtime PASS (8.7) + human review (8.6); blocked by 8.7 |

## Candidate results (simulation, 640 runs each)

| candidate | config hash | ttfa_p50 | ttfa_p95 | chunks | underruns | hard splits | protected fallbacks | preservation failures | finality failures |
|---|---|---|---|---|---|---|---|---|---|
| cand-01 (t=1800, se=1200, st=1200) | 2807af2c76ea | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-02 (t=1800, se=1200, st=1400) | 6b883a9da5f8 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-03 (t=1800, se=1500, st=1200) | 23c23361f284 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-04 (t=1800, se=1500, st=1400) | d4688c06d78c | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-05 (t=2200, se=1200, st=1200) | 09097c4b2b98 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-06 (t=2200, se=1200, st=1400) | 778a8ad73c24 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-07 (t=2200, se=1500, st=1200) | 20259c2591ba | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-08 (t=2200, se=1500, st=1400) | 6c4a5c541637 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-09 (t=2600, se=1200, st=1200) | 7787ebf8790d | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-10 (t=2600, se=1200, st=1400) | adbda7c15e06 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-11 (t=2600, se=1500, st=1200) | 11f735a71322 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-12 (t=2600, se=1500, st=1400) | d5fa795523c3 | 1100 | 1550 | 4018 | 1105 | 31 | 0 | 0 | 0 |
| cand-baseline-fixed (12/40/80/350ms) | 3fc7fe0fa3b8 | 1150 | 1550 | 3832 | 1140 | 12 | 0 | 0 | 0 |

Config abbreviations: t = target_duration_ms, se = startup_early_target_ms, st = starvation_target_ms. Other AdaptiveViPolicyConfig fields at module defaults.

## Key finding — candidate tuning knobs are currently dead config

Every adaptive candidate produces identical metrics (same ttfa/chunks/underruns per profile). Root cause (verified in src/backend/application/text_chunker/policy.py): soft_target_duration_ms() reads ONLY module-level constants (TARGET_DURATION_MS, STEADY_TARGET_MS, STARVATION_TARGET_MS, STARTUP_EARLY_TARGET_MS, watermarks, thresholds). The AdaptiveViPolicyConfig tuning fields (target_duration_ms, startup_late_elapsed_ms, startup_early_target_ms, starvation_watermark_ms, starvation_target_ms, steady_target_ms, healthy_watermark_ms, rtf_degraded_threshold, first_audio_slow_ms, min/max soft target) are declared but NOT consumed by the soft-target law. Only min_chars / max_chars / char_bias_chars are read from config.

Consequences:
- The runtime HINTS profiles DO change behavior (startup/starvation shrink the soft target via the module-constant law -> earlier weak commits -> TTFA p50 1100 vs steady/neutral 1550; chunk counts 1117/1096 vs 902/903). The adaptive mechanism responds to runtime signals.
- The benchmark's interpretable tuning surface (handoff 57: startup/steady/starvation duration targets) has zero offline signal until the soft-target law is wired to config. This is a calibration-readiness finding for the future VieNeu calibration task.
- This is a finding surfaced BY the benchmark; the runner intentionally does not modify policy (task 8.3 constraint).

## Offline TTFA note

Simulation TTFA = first-chunk emission instant + first-chunk synthesis cost (fixed 800 ms simulation constant, documented in runner docstring). Absolute numbers are NOT VieNeu-calibrated; only relative ordering (startup/starvation < steady) is meaningful offline. Full-text delivery is flat across profiles by construction (first chunk always emits on feed 1).

## Why NOT PASS (8.7/8.8)

The PASS rule (Decision 12 / handoff 30) requires, on the REAL VieNeu runtime: zero correctness failures, human prosody non-regression, and TTFA p95 <= +5% vs baseline among prosody-eligible candidates. Runtime unavailable -> no prosody-eligible candidate -> no candidate may win. fixed stays default; approved-script-authoring-pipeline remains BLOCKED. The failing dimension is environmental: vieneu package, local weights, and GPU are all absent (see runtime table); downloading weights from Vietnam is throttled to ~37KB/s (multi-hour, not authorized this session).

## Reproducibility metadata (handoff 56)

Per-run *-metrics.json embeds: policy name, policy/config hash, duration-estimator coefficients (frozen defaults), scorer weights (kind 1000000 / duration 1000 / char 1), corpus version 1, runner version, runtime mode, runtime report, ISO-8601 UTC run timestamp, candidate id. Re-run any row with:

    uv run --project services/product/backend_service python -m tests.unit.benchmark_fixtures.benchmark_runner --policy fixed --candidate-id cand-baseline-fixed --output benchmarks/adaptive-chunking
    uv run --project services/product/backend_service python -m tests.unit.benchmark_fixtures.benchmark_runner --policy adaptive_vi --candidate-id cand-01 --output benchmarks/adaptive-chunking

Mode B (real runtime), when the environment is authorized:

    VIENEU_RUNTIME=1 VIENEU_MODEL=pnnbao-ump/VieNeu-TTS-v3-Turbo VIENEU_WEIGHTS_PATH=<local> uv run --project services/product/backend_service python -m tests.unit.benchmark_fixtures.benchmark_runner --mode vieneu --output <dir>
