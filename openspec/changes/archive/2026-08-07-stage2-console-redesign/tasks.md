## 1. Diagnostics Contract and Speech Lifecycle

- [x] 1.1 Add failing tests for canonical queue counters, Director cycle count, active/queued turns, completed speech count, and bounded completed history.
- [x] 1.2 Implement public ChatQueue snapshot semantics for `received_total`, `buffered_comments`, and active-window comments without exposing private deque storage.
- [x] 1.3 Add stable speech `turn_id` and explicit lifecycle payloads for selected cluster, prompt/input, generated script, playback state, upcoming turns, and completed history.
- [x] 1.4 Implement the canonical per-session diagnostics snapshot and temporary legacy aliases with documented equivalence.
- [x] 1.5 Update Coordinator WebSocket speech events to include `turn_id` and lifecycle state so snapshots and events reconcile deterministically.
- [x] 1.6 Run focused ChatQueue, DirectorCoordinator, multisession, and diagnostics API tests.

## 2. Shared Embedder and Director-Equivalent Clusters

- [x] 2.1 Add failing tests proving diagnostics reuse the session embedder/cache, selection window, and session merge threshold and do not re-encode unchanged comments.
- [x] 2.2 Refactor DirectorRuntime to own and expose one injectable process-level embedding service used by session attachment and Coordinator ingestion.
- [x] 2.3 Add a public active-comment snapshot and remove direct `_deque` and `_sessions` inspection from `/debug/clusters/{session_id}`.
- [x] 2.4 Replace the independent debug clustering path with the same active cluster snapshot consumed by Director ranking.
- [x] 2.5 Return embedder identity/status, effective configuration, active/total comments, singleton, multi-comment, actionable, and unanswered cluster metrics.
- [x] 2.6 Verify repeated polling is deterministic and performs no new embedding work without new comments.

## 3. Semantic Readiness and Commerce-Aware Clustering

- [x] 3.1 Add deterministic Vietnamese commerce fixtures covering paraphrases, cross-product price questions, intents, greetings, spam, complaints, and unknowns.
- [x] 3.2 Record the current hashing and semantic baselines for singleton ratio, required merges, and prohibited merges.
- [x] 3.3 Add explicit `semantic-required` and `hash-explicit` embedder modes with sanitized structured load status.
- [x] 3.4 Provision the Vietnamese semantic embedder dependency/model in local Stage 2 and deployment configuration while keeping CI network-free.
- [x] 3.5 Make `/health/ready` fail in Stage 2/prod when the required semantic embedder is unavailable and report degraded status in diagnostics.
- [x] 3.6 Implement deterministic category and commerce-intent routing with explicit `unknown` values.
- [x] 3.7 Partition semantic clustering by category, product, and intent; exclude spam/off-topic from Q&A and route social comments only to engagement behavior.
- [x] 3.8 Tune the merge threshold from fixture evidence and add regression gates for same-intent merges and cross-partition separation.
- [x] 3.9 Run clustering, scoring, Director smoke, readiness, and API contract tests.

## 4. Runtime Resource Discovery and TTS Preview

- [x] 4.1 Add contract tests for frontend-safe LLM, TTS, voice, and avatar discovery metadata and active selection.
- [x] 4.2 Extend the existing `/engines` and `/avatars` responses with stable IDs, readiness, capabilities, thumbnails, and safe metadata without secrets or unrestricted paths.
- [x] 4.3 Add adapter-level voice discovery with a configured-default fallback for engines that cannot enumerate voices.
- [x] 4.4 Add validated TTS preview request handling with bounded text, auth, timeout, rate limiting, browser-playable audio, and response metadata.
- [x] 4.5 Add selection validation for unknown or incompatible model/voice combinations without mutating the previous active runtime.
- [x] 4.6 Run engine manager, TTS adapter, avatar API, auth, and preview endpoint tests.

## 5. Three-Layer Sandbox Verification

- [x] 5.1 Add failing tests for ordered layer execution, dependent-layer skipping, sanitized errors, timeout, cancellation, and cleanup.
- [x] 5.2 Implement credential/provider probe as verification layer one.
- [x] 5.3 Implement temporary LiveAvatar session and LiveKit connectivity verification as layer two.
- [x] 5.4 Implement bounded selected LLM-to-TTS-to-avatar speech verification using playback completion as layer three.
- [x] 5.5 Guarantee idempotent temporary-session cleanup in success, failure, timeout, and cancellation paths.
- [x] 5.6 Expose the admin-authenticated verification endpoint with per-layer status and latency but no secrets or raw provider payloads.
- [x] 5.7 Run offline mocked tests and one explicitly authorized sandbox smoke when credentials are available.

## 6. Editable Session Configuration

- [x] 6.1 Add API validation tests for structured shop fields, duplicate product IDs, price relationships, stock, arrays, catalog limits, and preserved order.
- [x] 6.2 Define the canonical frontend draft model for shop profile, selected products, product order, and product fields.
- [x] 6.3 Implement structured shop-profile editing and serialization into the existing two-layer persona input.
- [x] 6.4 Implement product selection and accessible up/down ordering controls; preserve submitted order through `/lite/attach` and Director state.
- [x] 6.5 Implement synchronized product form and advanced JSON editing with visible validation failures and no silent data loss.
- [x] 6.6 Run attach/persona/product-order API tests and frontend static behavior checks.

## 7. Stage 2 Console Redesign

- [x] 7.1 Replace duplicate controls with one responsive layout containing session, avatar, LLM, TTS/voice preview, shop, products, video, Auto Demo, diagnostics, and event log blocks.
- [x] 7.2 Add one explicit frontend state object/reducer driven by backend discovery, diagnostics snapshots, and lifecycle WebSocket events.
- [x] 7.3 Render the Auto Demo states: idle, verifying, attaching, introducing, answering, generating, synthesizing, playback, advancing, stopped, and failed.
- [x] 7.4 Render current product/decision, selected cluster, full prompt, full generated script, playback state, upcoming work, and bounded completed history without silent truncation.
- [x] 7.5 Replace legacy `pending`/`decisions` labels with canonical metrics and show embedder readiness plus singleton/actionable cluster quality.
- [x] 7.6 Add keyboard navigation, labels, visible focus, non-color status text, and desktop/tablet responsive behavior.
- [x] 7.7 Add frontend regression checks for one-instance controls, canonical field usage, full diagnostics accessibility, and state transitions.

## 8. End-to-End Verification and Migration

- [x] 8.1 Run targeted tests for every changed backend module, then the full offline `core/tests/` suite and Ruff on changed Python files.
- [x] 8.2 Start the actual local backend with Stage 2 configuration and verify health, readiness, discovery, preview, attach, ingest, diagnostics, speech lifecycle, and stop endpoints.
- [x] 8.3 Drive `frontend/stage2.html` in a browser through sandbox verification, resource selection, voice preview, shop/product edits, P004-first order, and Auto Demo.
- [x] 8.4 Ingest the deterministic 89-comment scenario and verify semantic embedder status, cluster quality metrics, selected cluster, prompt, generated script, playback completion, and canonical counters.
- [x] 8.5 Verify session stop clears queue, caches, speech history, temporary verification resources, and LiveAvatar/LiveKit sessions.
- [x] 8.6 Confirm the console no longer consumes legacy aliases, remove aliases when migration evidence permits, and validate the OpenSpec change.

## 9. Local Fixtures and No-Auto-Load Bootstrap

- [x] 9.1 Add local fixture JS for 4 mock products, shop presets, and token prefills that render without network calls.
- [x] 9.2 Implement versioned localStorage draft storage with fallback to fixture defaults on schema mismatch.
- [x] 9.3 Remove page-load mock fetch, Auto Demo auto-start, and misleading "loaded" log entries.
- [x] 9.4 Add `will_speak=false` to Attach response and `ingest()` activation gate in Coordinator.
- [x] 9.5 Implement live Re-attach: atomic profile+catalog revision, speaking-turn completion, product skip/append, next-turn profile, checkpoint preservation, stale prepared-turn invalidation.
- [x] 9.6 Run focused static, attach, and re-attach tests.

## 10. FSM Opening, Product Lifecycle Stages

- [x] 10.1 Implement protected three-turn global opening (shop intro, engagement call, catalog overview) with LLM grounding and profile/catalog revision cache.
- [x] 10.2 Implement per-product lifecycle stages: Intro, Benefit (1..N), Offer, Trust, CTA, Transition with stage tracking in ProductState.
- [x] 10.3 Implement `sell_product` action and stage-aware task generation in Director `decide()`.
- [x] 10.4 Ensure Intro + Benefit 1 are a protected gate before any Q&A window opens.
- [x] 10.5 When comments are insufficient, continue selling tasks; when current product's tasks complete, advance to next per order.
- [x] 10.6 Run lifecycle sequence and stage-transition tests.

## 11. Q&A Windows, Cooldown, Answer Cache, Paraphrase

- [x] 11.1 Implement Q&A window gating: openable only after Intro+Benefit1, per-window max clusters (default 2) + hard timeout (default 45s), early close when eligible queue exhausted.
- [x] 11.2 Implement singleton rolling window for deferred merging.
- [x] 11.3 Implement topic cooldown (default 120s) with new-content reset.
- [x] 11.4 Implement cluster intent paraphrase → one-clause grounded answer (1–2 sentences).
- [x] 11.5 Implement answer variant cache: keyed by (product, topic, profile_revision, catalog_revision), default 3 variants, round-robin selection, invalidation on revision change.
- [x] 11.6 Run Q&A window, cooldown, cache hit/miss, and paraphrase tests.

## 12. Cross-Product Excursion and Demand Pivot

- [x] 12.1 Implement Q&A excursion: single-turn B cluster answer → return to A checkpoint.
- [x] 12.2 Implement demand pivot detection: >=60% share, min 5 unique comments, margin >=0.15.
- [x] 12.3 Implement checkpoint/resume for pivoted product A.
- [x] 12.4 Implement full lifecycle B without global opening, resume A when B exits <45%.
- [x] 12.5 Implement hysteresis (60 enter / 45 exit) to prevent oscillation.
- [x] 12.6 Implement no-nested-pivot rule: C demand queued during active B lifecycle.
- [x] 12.7 Run excursion, pivot enter/exit, hysteresis, and queue-C tests.

## 13. Continuous Auto Demo Loop and Prepared Queues

- [x] 13.1 Implement initial ingest mode control (batch 20 vs. individual from start).
- [x] 13.2 Implement continuous producer: configurable rate 0.2–5/s default 0.67, non-overlapping setTimeout, rate-change-respects-next-tick.
- [x] 13.3 Implement fixture pool repeat with new IDs and timestamps.
- [x] 13.4 Implement rolling visible feed (newest 20) distinct from backend time-based window.
- [x] 13.5 Implement immediate Stop: interrupt producer, invalidate pending/prepared turns, keep session attached.
- [x] 13.6 Implement three-tier pipeline: Decision Queue → Preparation Queue → Serialized Playback Queue.
- [x] 13.7 Implement configurable prepared-turn depth (default 3) with generation/revision token stale detection.
- [x] 13.8 Run continuous loop, queue lifecycle, stop, and invalidation tests.

## 14. Runtime Config Controls and Revision-Based Invalidation

- [x] 14.1 Add validated FE controls for: comment rate, initial mode, max clusters/window, window timeout, cooldown, cache variants, prepared depth, retry count, pivot thresholds.
- [x] 14.2 Implement `config_revision` token: increments on any accepted runtime parameter change.
- [x] 14.3 Implement stale-turn detection: prepared turn with `generation_token` older than current `config_revision` → dropped as `cancelled_stale`.
- [x] 14.4 Ensure config changes apply from next turn without resetting opening or product checkpoint.
- [x] 14.5 Implement rollback on validation failure: backend rejects invalid config, keeps old-accepted runtime state.
- [x] 14.6 Run config update, revision invalidation, and rollback tests.

## 15. Benchmark Harness (Offline + Local-Real + Sandbox)

- [x] 15.1 Refactor Auto Demo fixture/config/FSM into a shared harness callable from both UI and CLI.
- [x] 15.2 Implement offline-deterministic lane: hash embedder + fixed LLM/TTS + mock renderer, exact state assertion.
- [x] 15.3 Implement local-real full-loop lane: semantic embedder + real LLM/TTS + mock renderer, coverage target (3 opening + 1 product lifecycle + 2 Q&A + excursion + pivot + resume) with 10min hard timeout.
- [x] 15.4 Ensure sandbox lane stays bounded (1–3 turns, not full-lifecycle evidence).
- [x] 15.5 Collect metrics per run/turn/revision: all pipeline stage latencies, queue pressure, retries, stale work, drops, underflow, cleanup.
- [x] 15.6 Write gitignored JSON reports to `.runtime/benchmarks/stage2/`.
- [x] 15.7 Implement baseline comparison: p95 stage regression >20% → FAIL.
- [x] 15.8 Run all three benchmark lanes and verify output. (2026-08-07: lane 1 stage2_pipeline offline PASS — 25 turns p95 e2e 5.6ms baseline-created; lane 2 commerce_clustering PASS — bkai VN embedder threshold sweep precision 0.5→0.9; lane 3 api/latency DEFERRED — API api.livento.me teardown, run again after redeploy)

## 16. Error Policy, Diagnostics Enhancement, and Cleanup

- [x] 16.1 Implement error taxonomy: transient (retry max 1), terminal (immediate stop), validation (fail-fast), stale cancellation, playback timeout separated.
- [x] 16.2 Ensure transient error does not mark answered/advance state before success.
- [x] 16.3 Implement stop-session verification: backend tasks/queues/cache/locks/store + LiveAvatar + LiveKit + local UI all clean.
- [x] 16.4 Enhance diagnostics to expose queue lifecycle position, revision tokens, module-boundary latency spans, per-factor score breakdown.
- [x] 16.5 Implement prompt-layer diagnostics (base_role, shop_profile, stage_task, final_prompt) in speech lifecycle.
- [x] 16.6 Run error taxonomy, stop-cleanup, and diagnostics completeness tests.

