## 1. OpenSpec and contract baseline

- [ ] 1.1 Record implementation baseline SHA and validate this change under the repository's `spec-driven` schema.
- [ ] 1.2 Add strict delta specs for all new/modified capabilities in this change.
- [ ] 1.3 Add a contract migration test that fails while any removed viewer-ingress route remains mounted.
- [ ] 1.4 Add a static test that fails if `websocket/platform.schema.json` remains generated after migration.
- [ ] 1.5 Add architecture guards preventing a second script-specific chunker or sentence=`TextChunk` coupling.

## 2. Canonical multi-platform event ingress

- [ ] 2.1 Define `PlatformEvent`, `ViewerRef`, typed payload models, validation limits, and stable event-id semantics.
- [ ] 2.2 Implement `POST /api/v1/sessions/{session_id}/events` accepting bounded one-or-many events.
- [ ] 2.3 Implement `PlatformEventIngestionService` below HTTP transport.
- [ ] 2.4 Implement bounded/durable session event-id deduplication and idempotent duplicate responses.
- [ ] 2.5 Normalize stable viewer identity fields needed for unique-viewer demand.
- [ ] 2.6 Persist accepted/rejected event metadata through existing repository/store boundaries without blocking semantic processing on optional diagnostics persistence.
- [ ] 2.7 Route non-comment canonical events to traffic/session signals without embedding them.
- [ ] 2.8 Add multi-platform, burst, duplicate, retry, reordered, and malformed ingestion tests.
- [ ] 2.9 Remove `/ws/platform/{session_id}`.
- [ ] 2.10 Remove `/sessions/{session_id}/ingest`.
- [ ] 2.11 Remove `/sessions/{session_id}/chat`.
- [ ] 2.12 Remove the synchronous Director ingest fallback used by the deleted API surface.
- [ ] 2.13 Remove platform WebSocket schema generation/artifact and regenerate backend contracts.

## 3. Safety Gate before embedding

- [ ] 3.1 Define versioned `SafetyDecision` and policy reason codes.
- [ ] 3.2 Implement malformed/replay-flood/spam checks.
- [ ] 3.3 Add curated profanity/toxicity/harassment/unsafe-content resources with provenance/license metadata.
- [ ] 3.4 Add deterministic prompt-injection pattern handling as an additional viewer-content safety signal.
- [ ] 3.5 Prove rejected comments never reach embedder calls, cluster membership, demand counts, or Agent context.
- [ ] 3.6 Add sanitized safety counters to diagnostics.
- [ ] 3.7 Add fixture coverage for Vietnamese slang/diacritics and false-positive regression.

## 4. Event-driven fast reducer

- [ ] 4.1 Introduce event/condition-driven reducer wakeup instead of unconditional fixed polling.
- [ ] 4.2 Add typed `microbatch_max_wait_ms` config and batch new accepted comments for embedding.
- [ ] 4.3 Preserve one embedding per accepted comment identity/revision.
- [ ] 4.4 Separate rolling-demand horizon config from microbatch timing.
- [ ] 4.5 Add deterministic fast-lane latency benchmark from accepted event to updated ranked demand.
- [ ] 4.6 Prove idle sessions do not wake at the old fixed polling cadence when no work/deadline exists.

## 5. Stable ClusterStore and reconciliation

- [ ] 5.1 Define `LiveCluster`, stable `cluster_id`, representative, viewer-demand, product-resolution, novelty, and lifecycle fields.
- [ ] 5.2 Implement bounded per-session `ClusterStore`.
- [ ] 5.3 Implement incremental semantic assignment/update from newly embedded comments.
- [ ] 5.4 Implement member/embedding expiry tied to active rolling horizon.
- [ ] 5.5 Implement reconciliation trigger state: `>=100` unreconciled comments OR `>=60s` since first unreconciled comment.
- [ ] 5.6 Implement bounded active-horizon reconciliation capable of compatible merge/split/centroid/medoid repair.
- [ ] 5.7 Preserve last valid fast-lane state on reconciliation failure and emit typed diagnostics.
- [ ] 5.8 Add stable-ID and arrival-order benchmark fixtures.
- [ ] 5.9 Add long-duration bounded-memory test proving state does not grow linearly with livestream duration.
- [ ] 5.10 Remove parallel unbounded rolling-comment/embedding history no longer required by the new store.

## 6. Soft routing, multi-product resolution, and ranking

- [ ] 6.1 Replace hard single-product pre-cluster routing with `RoutingHints`.
- [ ] 6.2 Preserve strong explicit product-id/alias matches as high-confidence candidates.
- [ ] 6.3 Add cluster-level product resolver with confidence threshold and top-candidate margin.
- [ ] 6.4 Represent zero/one/many resolved product IDs.
- [ ] 6.5 Add deterministic comparison-question fixtures that resolve multiple products without accidental merge rejection.
- [ ] 6.6 Add unique-viewer count to cluster state and make it the primary demand popularity signal.
- [ ] 6.7 Add repeated-single-viewer anti-inflation tests.
- [ ] 6.8 Replace pivot share calculations with active unique-viewer demand.
- [ ] 6.9 Select medoid/diversity representatives instead of first-N arrival members.
- [ ] 6.10 Replace coarse `product:intent` cooldown/cache identity with stable semantic topic/cluster fingerprint inputs.
- [ ] 6.11 Make skip/selection lifecycle state persist on stable cluster identities.

## 7. ClusterEnvelope boundary

- [ ] 7.1 Define canonical `ClusterEnvelope`.
- [ ] 7.2 Ensure Director/Agent receives only selected envelopes, not the uncontrolled rolling raw comment list.
- [ ] 7.3 Include ranking score breakdown, representative questions, unique viewers, product candidates/resolution, novelty, and current script product.
- [ ] 7.4 Add static/unit checks preventing raw full-window comments from being appended automatically to Agent prompts.
- [ ] 7.5 Update cluster diagnostics and Workbench to show the exact envelope used for each Q&A decision.

## 8. Universal commerce entity context

- [x] 8.1 Define `EntityDocument`, `Fact`, `KnowledgeBlock`, and `Relation`.
- [x] 8.2 Define Common Fact Registry with canonical commerce/identity keys, aliases, types, and freshness policy.
- [x] 8.3 Support arbitrary custom facts without Python/TypeScript schema changes.
- [x] 8.4 Implement revisioned entity repository using the existing persistence stack's document/JSON semantics unless design evidence proves a separate datastore is required.
- [x] 8.5 Implement entity search by id/name/alias/tags and fact selectors.
- [x] 8.6 Implement query-relevant context rendering rather than full-document serialization.
- [x] 8.7 Migrate Director catalog/retrieval/fact answering.
- [x] 8.8 Migrate session attach/snapshots/run-plan inputs.
- [x] 8.9 Migrate script-authoring authoritative context/fingerprints without weakening approval freshness.
- [x] 8.10 Migrate backend API models and generated OpenAPI.
- [x] 8.11 Migrate Workbench TypeScript types/fixtures/UI.
- [ ] 8.12 Remove rigid product/shop compatibility adapters after all consumers migrate.
- [x] 8.13 Add cross-domain fixtures for fashion, cosmetics, food, electronics, household goods, and at least one custom vertical.

## 9. Shop/Product Data Studio

- [ ] 9.1 Add simple common-field form.
- [ ] 9.2 Add arbitrary user-facing label/value rows mapped through the fact registry.
- [ ] 9.3 Preserve unknown labels as custom facts rather than rejecting them.
- [ ] 9.4 Add raw/pasted knowledge blocks.
- [ ] 9.5 Add optional AI extraction suggestions without making extraction a save dependency.
- [ ] 9.6 Require explicit user acceptance before suggested facts become authoritative.
- [ ] 9.7 Add advanced normalized entity document view.
- [ ] 9.8 Show exact evidence/context rendering preview.

## 10. Evidence Planner and cache

- [ ] 10.1 Define typed `EvidenceRequest`, `EvidenceBundle`, and freshness metadata.
- [ ] 10.2 Implement generic `search_entities`, `get_entities`, and batched `get_evidence` application operations.
- [ ] 10.3 Implement `EvidenceCache` keyed by entity/selector/revision/freshness semantics.
- [ ] 10.4 Implement cache-first planner that batches only misses.
- [ ] 10.5 Execute independent evidence misses concurrently where safe.
- [ ] 10.6 Define shorter freshness or explicit invalidation for price/stock/promotion/availability.
- [ ] 10.7 Add cache-hit, partial-hit, stale, revision-change, and volatile-refresh tests.
- [ ] 10.8 Add diagnostics for requested selectors, cache hit/miss counts, freshness state, and batch fan-in without leaking unnecessary private content.

## 11. Structured Agent memory

- [ ] 11.1 Implement bounded `ScriptState`.
- [ ] 11.2 Implement bounded structured `SessionMemory`.
- [ ] 11.3 Implement keyed `TopicMemory` for recent Q&A/reference resolution.
- [ ] 11.4 Keep `EvidenceCache` independent from LLM conversation turns.
- [ ] 11.5 Add deterministic eviction/token-budget policy.
- [ ] 11.6 Prove full runtime transcript persistence is not automatically replayed into model context.
- [ ] 11.7 Add follow-up fixture such as “vậy cái đó có sạc nhanh không?” resolving through bounded topic/entity memory.

## 12. Bounded Agentic Director

- [ ] 12.1 Define typed Agent plan/result contracts above the existing model-agnostic LLM seam.
- [ ] 12.2 Implement deterministic factual fast-path eligibility.
- [ ] 12.3 Support zero-LLM exact templated factual answers where appropriate.
- [ ] 12.4 Support one-generation grounded verbalization for factual answers requiring natural phrasing.
- [ ] 12.5 Implement complex path with maximum one planning generation, one normal evidence round, and one final answer generation.
- [ ] 12.6 Add configurable exceptional ceiling of a second evidence round; reject attempts beyond the budget.
- [ ] 12.7 Validate all model-requested evidence operations against an allowlisted typed schema.
- [ ] 12.8 Forbid arbitrary filesystem/web/job-management/tool execution.
- [ ] 12.9 Ensure backend code, not model output, owns retries, candidate selection, pivot policy, script cursor state, and job creation.
- [ ] 12.10 Add exact call/round/token/latency telemetry.
- [ ] 12.11 Add ambiguous, comparative, multi-product, open-ended, and referential Q&A fixtures.
- [ ] 12.12 Add hallucination regression proving unavailable authoritative evidence is never replaced by invented exact facts.

## 13. Approved-script sentence cursor

- [ ] 13.1 Add deterministic sentence-map derivation from exact approved `spoken_text`.
- [ ] 13.2 Prove sentence-span concatenation preserves the exact approved artifact.
- [ ] 13.3 Persist/runtime-store script-set id, approved version, product id, current sentence index, last completed sentence, and exact next sentence.
- [ ] 13.4 Speak each approved sentence through the existing canonical `speak_verbatim` path.
- [ ] 13.5 Advance cursor only after normal sentence-level speech completion.
- [ ] 13.6 Do not infer sentence completion from individual TextChunk boundaries.
- [ ] 13.7 Preserve Change A ownership of chunk policy, deadlines, hints, and finality.
- [ ] 13.8 Preserve Change B approval/version immutability and no post-approval rewrite.

## 14. Speech Arbiter and pending Q&A

- [ ] 14.1 Implement explicit arbiter state machine.
- [ ] 14.2 Make active approved script sentence non-preemptible for normal Q&A.
- [ ] 14.3 Continue reducer processing while the sentence is playing.
- [ ] 14.4 Maintain bounded pending-Q&A candidates with score hysteresis/supersession.
- [ ] 14.5 Revalidate winner at safe sentence boundary.
- [ ] 14.6 Defer expensive final Agent generation until boundary revalidation by default.
- [ ] 14.7 Allow stable evidence prefetch for high-confidence pending candidates.
- [ ] 14.8 Revalidate volatile evidence just-in-time before speech.
- [ ] 14.9 Speak Q&A, preserve checkpoint at exact next script sentence, then resume.
- [ ] 14.10 Keep operator/emergency hard interrupt as a distinct control-plane path.
- [ ] 14.11 Add P010-script/P020-Q&A fixture proving P010 current sentence completes, P020 Q&A speaks, and P010 resumes at exact next sentence.
- [ ] 14.12 Add Q&A failure fixture proving script cursor remains valid and runtime resumes per policy.

## 15. Natural lead-in and resume transitions

- [ ] 15.1 Add deterministic Vietnamese Q&A lead-in templates based on cluster/topic/product.
- [ ] 15.2 Allow final answer generation to include the lead-in in the same call.
- [ ] 15.3 Add deterministic resume templates using current script product and optional sentence metadata.
- [ ] 15.4 Do not add a dedicated bridge-only LLM call to the normal path.
- [ ] 15.5 Add Vietnamese naturalness fixtures and exact-fact preservation checks.

## 16. Workbench SE Adapter Simulator

- [ ] 16.1 Replace direct legacy single/batch viewer injection as the primary integration model.
- [ ] 16.2 Simulate concurrent TikTok/Shopee/Facebook/YouTube sources.
- [ ] 16.3 Add per-platform rate/burst/batch/jitter/out-of-order controls.
- [ ] 16.4 Add retry using identical event IDs.
- [ ] 16.5 Add malformed-event and source-outage/recovery scenarios.
- [ ] 16.6 Display simulated source payload and exact canonical `/events` request.
- [ ] 16.7 Add deterministic replay fixtures suitable as an SE integration reference.

## 17. Workbench runtime inspectors

- [ ] 17.1 Add Safety Gate counters/reason inspector.
- [ ] 17.2 Add stable cluster/representative/unique-viewer/product-confidence inspector.
- [ ] 17.3 Show fast-lane and reconciliation trigger state.
- [ ] 17.4 Show exact selected ClusterEnvelope.
- [ ] 17.5 Show SessionMemory/TopicMemory/EvidenceCache metadata.
- [ ] 17.6 Show evidence plan, cache hit/miss, batched fetches, and LLM/tool round count.
- [ ] 17.7 Show bound script version, current sentence, last completed sentence, and next sentence.
- [ ] 17.8 Add speech-arbiter timeline with script/Q&A/resume events.

## 18. Optional agent context-compression benchmark

- [ ] 18.1 Define all-text baseline fixture set using the actual target vision-capable model.
- [ ] 18.2 Define hybrid mode that keeps instruction/control/dynamic exact facts as text and renders only eligible read-only descriptive context as images.
- [ ] 18.3 Record effective/model-reported input tokens, TTFT, total latency, and cost.
- [ ] 18.4 Measure exact number/identifier accuracy, Vietnamese diacritics, grounding, tool selection, and hallucination.
- [ ] 18.5 Define non-regression thresholds and a minimum material token/latency benefit.
- [ ] 18.6 Keep hybrid mode disabled if thresholds are not met.
- [ ] 18.7 Ensure image context is never used to carry tool schemas, response schemas, instruction hierarchy, or authoritative volatile facts.

## 19. Contract cleanup and verification

- [ ] 19.1 Remove obsolete Workbench/API types and fixtures for deleted viewer-ingress contracts.
- [ ] 19.2 Remove obsolete `initial_ingest_mode` public/runtime configuration.
- [ ] 19.3 Regenerate deterministic backend OpenAPI and surviving control WebSocket schema.
- [ ] 19.4 Update docs/reference integration contract for SE.
- [ ] 19.5 Run focused unit/integration tests for each subsystem.
- [ ] 19.6 Run deterministic cluster/reducer benchmarks.
- [ ] 19.7 Run script Q&A/resume end-to-end tests through the canonical TextChunker/TTS/backend path.
- [ ] 19.8 Run Workbench tests/build.
- [ ] 19.9 Run contract generation diff check.
- [ ] 19.10 Run strict OpenSpec validation.
- [ ] 19.11 Verify repository search finds no mounted deleted ingress route, platform WS contract artifact, rigid entity compatibility adapter, or script-specific chunker path.
