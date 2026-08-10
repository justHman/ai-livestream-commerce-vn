# VieNeu benchmark fixtures (adaptive speech-text chunking 8.1-8.2)

Versioned, authored-synthetic Vietnamese benchmark corpus plus deterministic
streaming-fragment generation for the fixed-versus-adaptive chunking
comparison. Consumable by the future benchmark runner without backend
dependencies.

## Files

- `vi_benchmark_corpus_v1.json` — corpus version 1: exactly 40 authored
  utterances, exactly 4 per category across the 10 categories
  (short/long conversational speech, clauses, multi-sentence paragraphs,
  prices/currency/percent, decimal/grouped numbers, product names/SKUs,
  acronyms, mixed VI/EN, complete scripts). Each entry has a stable slug
  `id`, a `category`, and exact UTF-8 `text`. Authored edge coverage:
  balanced Vietnamese quotes, balanced parentheses, a reserved
  non-resolving URL (`https://shop.example.invalid/...`) and email
  (`hotro@example.invalid`), a literal escaped newline, tabs, repeated
  spaces, decimals/grouped numbers, prices/currency/percent, fictional
  product names and SKUs, acronyms, and mixed VI/EN texts.
- `fragments.py` — stdlib loader and deterministic fragment generators:
  `full` (one fragment), `character` (one codepoint), `word`
  (whitespace-preserving, never lossy `split()`), `provider_like`
  (word-aligned coalesced deltas in a 3/1/2 lexical-word pattern,
  non-empty, no mid-word cuts). The loader validates schema/version,
  top-level shape, item schema and types (exact key set), ID pattern,
  unique IDs, unique non-empty texts, declared category consistency
  (non-empty, unique, string), and category coverage. `load_utterances`
  returns records in original JSON order; `load_by_category` groups
  without changing that order. Every delivery form reconstructs the
  source exactly: `"".join(fragments) == text`.
- `test_benchmark_fixtures.py` — contract tests: schema/version,
  provenance metadata, exact size and per-category counts, edge-form
  coverage, loader rejection of malformed corpus files, determinism,
  exact codepoint and UTF-8 byte reconstruction, maximal whitespace-run
  tokenization (`\s+|\S+`), provider multi-word coalescing, no empty
  fragments, short/whitespace-only edge cases, immutability, and
  reasonable fixture size.

## Provenance

Corpus is **authored synthetic** example commerce speech for benchmarking
only. It contains no PII, no real product claims, no real brand names, and
no factual ground-truth commerce assertions — prices, SKUs, product names,
and the URL/email forms are fictional demo content on reserved
`.invalid` domains. The `provenance` object in the corpus file states the
same contract in machine-checkable form.

## Versioning

Bump `VERSION` (and the `version` field / file name in
`vi_benchmark_corpus_v1.json`) whenever the corpus changes; `fragments.py`
rejects schema/version mismatches at load time.
