# Optional hybrid context-compression benchmark (OpenSpec section 18)

Deterministic, offline-safe benchmark harness for the optional hybrid
image-context design (Decision 21): the same Q&A fixture set is answered in
all-text and hybrid modes, correctness and efficiency are measured, and a
pure gate decides whether hybrid mode may be enabled. The real-model run is
operator-invoked only — CI runs the deterministic simulation mode.

## Files

- `vi_context_fixtures_v1.json` — versioned corpus version 1: 6 read-only
  descriptive context chunks (2 long product descriptions, 2 shop
  story/persona backgrounds, 2 campaign backgrounds) plus 15 Q&A fixtures
  across the 5 required task classes (exact number/identifier, Vietnamese
  diacritics, grounding, tool selection, hallucination-prone). Authored
  synthetic, no PII, no real product claims; the `provenance` object states
  the same contract in machine-checkable form.
- `benchmark_runner.py` — harness: corpus loader with provenance/schema
  validation, hybrid context classification (18.2), deterministic scoring
  (18.4), measurement records (18.3), simulation-mode fakes, operator seam
  + `CC_BENCH_RUNTIME=1` env gate for the real model (18.1), typed
  thresholds + pure enablement gate (18.5/18.6), JSON + Markdown report
  with NOT-PASS gate, and a minimal CLI.
- `guards.py` — task 18.7 static guards: prove the image-eligible corpus
  entries (and the harness source) contain no tool/response-schema markers,
  instruction-hierarchy markers, or volatile fact keys.
- `test_context_compression_benchmark.py` — deterministic tests: corpus
  parse + provenance + schema version, classification, measurement record
  shape + serialization, every scorer, gate decisions, end-to-end simulation
  run + report (NOT-PASS gate), and the real-mode env-gate fail-loud.
- `test_context_compression_guards.py` — 18.7 guard tests (positive on the
  real tree, negatives on simulated corpus files).

## Usage

Simulation mode (deterministic, no network — CI-safe):

```bash
uv run --project . python -m tests.unit.context_compression_benchmark.benchmark_runner \
  --mode simulation --output benchmarks
```

Real-model mode (operator-invoked on a machine with model access):

```python
from tests.unit.context_compression_benchmark.benchmark_runner import (
    set_real_seam,
    run_real,
    write_report,
)


class MyModel:  # implements the VisionModel Protocol
    def run_text(self, prompt: str) -> dict: ...  # model-reported tokens/TTFT/latency/cost


set_real_seam(MyModel())
```

then opt in with `CC_BENCH_RUNTIME=1` (and optionally `CC_BENCH_MODEL` to
name the target model; the default is `gpt-4o`). The report is written via
`write_report(baseline, hybrid, thresholds, output_dir)`.

## Acceptance rules (18.5/18.6)

Hybrid mode stays off unless a pure gate passes every threshold: accuracy
deltas (exact/diacritics/grounding/tool selection) must not drop more than
the configured floor (`-0.05`), the hallucination-rate delta must not exceed
the ceiling (`+0.05`), and the input-token and total-latency reductions must
meet the material minimums (`0.10` each, tunable). The gate is the only
thing that enables hybrid mode — the production default is all-text.

## Provenance

Corpus is authored synthetic example commerce content for benchmarking only.
It contains no PII, no real product claims, no real brand names, and no
factual ground-truth commerce assertions; prices/SKUs/names are fictional
demo content.

## Versioning

Bump `VERSION` (and the `version` field / file name) whenever the corpus
changes; the loader rejects schema/version mismatches at load time, and the
18.7 guards re-run over every revision.
