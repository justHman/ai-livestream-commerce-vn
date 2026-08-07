## Why

The completed Stage 2 batches established truthful diagnostics, semantic commerce clustering, editable session configuration, resource discovery, and sandbox verification, but operator acceptance still exposes three structural gaps: page load performs hidden mock fetches, Auto Demo sends only one 20-comment batch, and speech is prepared only after the previous avatar turn ends. The console and Director therefore do not yet behave like a continuous, demand-driven livestream: shop opening, product stages, ranked Q&A, cross-product demand, live configuration changes, and module-level latency cannot be exercised or verified as one coherent loop.

## What Changes

- Render local test tokens, shop presets, and the initial mock catalog without page-load network requests or misleading “loaded” events; require explicit Start and Attach before Auto Demo.
- Make Attach/Re-attach apply validated shop-profile and ordered-catalog revisions atomically while preserving the active opening/product checkpoint; expose accepted snapshots and revisions in diagnostics.
- Add realtime validated runtime controls for Auto Demo rate/seed mode, Q&A windows, topic cooldown, answer-cache variants, prepared-turn depth, and demand-pivot thresholds.
- Replace the one-shot Auto Demo batch with a cancellable continuous producer: optionally seed 20 comments as one batch, then ingest comments at a configurable rate while the visible feed retains only the newest 20.
- Add a protected three-turn global opening, followed by per-product Intro, Benefit, Offer, Trust, CTA, Q&A-window, and Transition stages. Intro plus the first Benefit complete before product Q&A can play.
- Rank only eligible multi-comment commerce clusters for speech, paraphrase the shared question before grounded answers, cache configurable answer variants, and reuse them round-robin after cooldown when new demand recurs.
- Support one-turn cross-product Q&A excursions and hysteresis-based demand pivots that checkpoint the current product, run the demanded product lifecycle, and resume immediately when demand cools.
- Split scheduling into bounded decision, preparation, and serialized playback queues so LLM/TTS (and self-host rendering where applicable) can prepare future turns during active avatar playback without overlapping speech.
- Invalidate stale prepared work by revision/generation token when profile, catalog, runtime configuration, pivot state, or Stop changes; classify transient, terminal, validation, stale-cancellation, and playback-acknowledgement failures explicitly.
- Add a reusable full-loop benchmark harness with offline deterministic, local-real, and bounded sandbox-smoke lanes; record stage latency, queue pressure, retries, stale work, playback acknowledgements, cleanup, and baseline regressions without running paid/non-sandbox sessions.

## Capabilities

### New Capabilities

- None. This revision completes the existing Stage 2 capabilities rather than introducing a separate product surface.

### Modified Capabilities

- `director-diagnostics`: Add ranked-cluster score breakdown, revisioned prepared/playback lifecycle, cache and pivot state, and module-boundary latency.
- `commerce-comment-clustering`: Add multi-comment Q&A eligibility, recurrent-topic cooldown/cache semantics, cross-product excursion, and demand-pivot behavior.
- `runtime-resource-discovery`: Extend runtime selection with validated per-session scheduling configuration and accepted revision snapshots.
- `editable-session-configuration`: Add local versioned drafts and atomic live Re-attach of profile/catalog without resetting the active checkpoint.
- `sandbox-verification`: Restrict sandbox to bounded smoke evidence and distinguish it from full-loop local-real verification.
- `stage2-operator-console`: Remove hidden startup fetches, add explicit Attach/Re-attach, continuous rate-configurable Auto Demo, complete queue/cluster/pivot diagnostics, and runtime controls.

## Impact

- Frontend: `frontend/stage2.html` draft persistence, local fixtures, Attach/Re-attach flow, continuous producer, runtime controls, diagnostics, cancellation, and event rendering.
- Backend API: `core/api/v1.py` attachment/runtime-config revision contracts, atomic update validation, diagnostics, benchmark hooks, and cleanup semantics.
- Director: state, run plan, ranking, Q&A windows, answer cache, product checkpoints, excursion/pivot hysteresis, and stage completion in `core/director/` and `core/schemas/run_plan.py`.
- Speech pipeline: bounded decision/preparation/playback queues, generation-token cancellation, cloud PCM preparation, self-host media preparation, and latency spans in `core/render/`, `core/tts/`, `core/llm/`, and `providers/liveavatar_cloud/`.
- Verification: revised focused contracts, full offline suite, local-real full-loop benchmark output under gitignored `.runtime/benchmarks/stage2/`, bounded authorized sandbox smoke, and final local servers started without automatically opening a browser.
- Compatibility: existing completed Stage 2 behavior remains additive where possible; old one-shot Auto Demo and page-load mock-fetch contracts are intentionally replaced. No `/user/*` or `/shop/*` business APIs, new UI framework, paid LiveAvatar session, deployment, or media-plane redesign is included.
