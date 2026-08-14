## Context

Audit basis: repository `justHman/ai-livestream-commerce-vn`, `main` at `b8c74538d838e7658f85e45caf6235e0661e4869`.

The current source establishes several architectural facts that constrain this design:

- viewer traffic has three public ingestion surfaces with overlapping behavior;
- Coordinator keeps one `ChatQueue` per session, polls on a fixed tick, embeds only new comment IDs, and stores routed comments/embeddings in session state;
- `cluster_comments()` greedily reconstructs clusters and partitions by `(category, product_id, intent, actionable)`;
- `rank_clusters()` performs product retrieval only when routing did not already set a product;
- pivot demand uses raw product-id occurrences;
- `Product` and API/Workbench product models are rigid commerce schemas;
- the LLM seam is general text generation and streaming, not an application-safe agent/tool runtime;
- approved-script runtime handoff resolves one immutable whole-product `spoken_text`;
- `StreamOrchestrator.speak_verbatim()` receives whole text, runs it through the canonical source-agnostic `TextChunker`, and owns phrase/TTS/video finality;
- `TextChunker` emits phrase-sized chunks and is not a sentence-level script scheduler.

The design therefore keeps deterministic reduction and speech scheduling outside the LLM, and adds agent behavior only after compact cluster selection.

## Goals

- One canonical SE→AI event ingress for all platforms.
- Idempotent retries and stable normalized viewer identity.
- Safety rejection before embedding.
- Low-latency incremental clustering without fixed idle polling.
- Stable bounded cluster identities plus periodic reconciliation.
- Unique-viewer-aware demand and multi-product cluster support.
- Cross-domain shop/product knowledge without a new rigid schema for each vertical.
- Bounded Agent memory and evidence retrieval independent of LLM transcript retention.
- Code-owned tool/LLM budgets.
- Exact approved-script sentence resume after reactive Q&A.
- No mid-sentence approved-script interruption.
- Workbench as an executable SE integration and runtime-observability harness.
- No compatibility aliases for replaced ingress/entity contracts.

## Non-Goals

- Platform-specific API adapters in the AI service.
- Replacing deterministic clustering/ranking with an LLM.
- Letting an LLM control retries, scheduling, pivot policy, or script cursor state.
- Rewriting approved scripts at runtime.
- Treating TextChunk boundaries as approved-script sentence boundaries.
- Introducing a mandatory new operational datastore solely for schema flexibility.
- Making multimodal prompt compression a required production dependency.

## Decision 1: One canonical `PlatformEvent` ingress

The public contract is:

```text
POST /api/v1/sessions/{session_id}/events
```

The request accepts a bounded list of `PlatformEvent` values. A one-event request uses the same schema and service path as a multi-event request.

Conceptual event model:

```text
PlatformEvent
  event_id: str
  platform: str
  source_stream_id: str
  occurred_at: datetime|epoch
  type: enum|string
  viewer?: ViewerRef
  payload: discriminated object
```

Initial canonical types SHOULD cover at least `viewer.comment`, `viewer.join`, `viewer.follow`, and `viewer.like`. Only comment-like events enter semantic reduction. Non-comment events can update traffic/session signals without being embedded.

`event_id` is the retry/idempotency identity. The ingestion service maintains a bounded session-scoped dedup index with durable/recoverable semantics appropriate to the session store. A duplicate accepted event returns an idempotent result and does not repeat persistence/embedding/demand effects.

No code above the transport boundary contains TikTok/Shopee/Facebook/YouTube protocol logic.

## Decision 2: Ingestion pipeline is application-owned

```text
HTTP /events
  -> PlatformEventIngestionService
      -> validate/dedup
      -> SafetyGate
      -> normalize ViewerIdentity
      -> persist accepted/rejected event metadata
      -> enqueue accepted semantic items
      -> signal FastReducer
```

The FastReducer is notified by an event/condition/channel when semantic work exists. It does not wake every 300 ms merely to discover no work.

A configurable `microbatch_max_wait_ms` provides a short coalescing window to batch embedding calls under burst traffic. The target default may start near the historical 300 ms but MUST be benchmarked; it is a maximum delay, not a periodic tick.

## Decision 3: Safety Gate precedes embedding

The Safety Gate runs deterministic/local checks before any embedding or Agent context creation.

```text
SafetyDecision
  accepted: bool
  reason_codes[]
  policy_version
  sanitized_metrics
```

Rules are versioned and curated. Rejected content can be persisted in a restricted/sanitized audit form subject to existing privacy policy, but no rejected text is embedded or passed to the Agent.

Prompt-injection detection is not considered a complete security boundary by itself. The stronger boundary is structural: viewer text is always untrusted data inside a compact `ClusterEnvelope`; it can never select system prompts, tools, retries, or execution policy.

## Decision 4: Separate fast lane, rolling horizon, reconciliation

These are three independent clocks/policies.

### Fast lane

Triggered by accepted semantic work. Performs batch embedding of new accepted comments, candidate routing hints, incremental cluster assignment/update, score refresh, and pending-Q&A notification.

### Rolling horizon

Defines active demand for ranking and pivot, initially `75s`. Membership older than the horizon no longer contributes to active demand. Expiry also drives bounded state cleanup.

### Reconciliation

Trigger state per session:

```text
unreconciled_count
first_unreconciled_at
```

Run when:

```text
unreconciled_count >= 100
OR
now - first_unreconciled_at >= 60s
```

Reconciliation can rebuild only the bounded active horizon, not whole-session history. Its goals are cluster quality and order robustness, not live responsiveness.

## Decision 5: Stable `ClusterStore`

Each session owns a bounded `ClusterStore`.

```text
LiveCluster
  cluster_id
  created_at
  updated_at
  centroid
  medoid_comment_id
  representative_comment_ids[]
  member_ids[]
  viewer_ids[]
  message_count
  unique_viewer_count
  intent_distribution
  product_candidates[]
  resolved_product_ids[]
  product_resolution_confidence
  cohesion
  newest_t
  last_selected_at?
  last_answered_at?
  novelty_fingerprint
  skip_count
```

The store exposes active clusters and mutation operations; Director does not reconstruct clusters from raw rolling comments on every decision.

Embeddings and member identities are evicted when no active/reconciliation requirement needs them. Memory tests MUST prove session memory remains bounded over long synthetic streams.

Stable `cluster_id` enables real skip/cooldown/answer lifecycle state.

## Decision 6: Soft candidate routing before clustering

Current deterministic lexical routing remains useful but changes role.

```text
RoutingHints
  intent_candidates[]
  product_candidates[(id, score, evidence)]
  category
  actionable
```

Strong explicit product IDs/aliases can produce high-confidence candidates. Weak references such as “cái này” remain ambiguous.

Incremental cluster assignment uses semantic similarity plus compatibility constraints, but MUST NOT require identical single `product_id` values produced by the first comment.

After clustering, cluster-level evidence resolves zero, one, or many products. If no candidate clears confidence/margin gates, ambiguity is preserved for the Agent/evidence planner rather than silently assigning top-1.

## Decision 7: Unique viewers are first-class demand

Demand scoring distinguishes `message_count` and `unique_viewer_count`. Unique-viewer demand is the primary popularity signal. Repeated messages from one viewer can increase recency/urgency slightly but cannot linearly emulate independent demand.

Conceptually:

```text
score =
    w_unique_viewers * f(unique_viewer_count)
  + w_message_demand * g(message_count)
  + w_intent * intent_actionability
  + w_recency * recency
  + w_script_relevance * current_script_product_relevance
  + w_product_confidence * product_resolution_confidence
  + w_novelty * novelty
```

Exact functions/weights remain typed configuration and benchmark targets. Pivot share also uses active unique-viewer demand for resolved product targets rather than raw comment count.

## Decision 8: Semantic representatives, not first-N arrival order

For Agent context, a cluster selects a small representative set: medoid closest to centroid, optionally one or two diversity representatives maximizing semantic distance subject to cohesion, and a bounded representative count.

The Agent never needs all cluster member texts for normal Q&A.

## Decision 9: `ClusterEnvelope` is the Agent boundary

```text
ClusterEnvelope
  cluster_id
  intent
  message_count
  unique_viewer_count
  representative_questions[]
  product_candidates[]
  resolved_product_ids[]
  ranking_score
  score_breakdown
  novelty
  current_script_product_id
  source_platform_counts
```

The envelope is untrusted evidence. It cannot carry model instructions, tool schemas, or mutable runtime authority. The Agent receives only the selected envelope(s) required for the current decision.

## Decision 10: Universal entity document

```text
EntityDocument
  id
  entity_type
  revision
  name
  aliases[]
  tags[]
  facts[]
  knowledge_blocks[]
  relations[]
```

A `Fact` contains key, type, value, optional unit/labels, revision/freshness/source metadata. `KnowledgeBlock` carries long revisioned prose and tags. `Relation` links another entity with typed metadata.

The core is intentionally small. Vertical-specific attributes use facts/knowledge blocks rather than code changes.

## Decision 11: Common Fact Registry

Code may require semantic concepts such as:

- `commerce.price.current`;
- `commerce.price.original`;
- `commerce.stock.available`;
- `commerce.stock.quantity`;
- `commerce.promotion`;
- `commerce.shipping`;
- `commerce.warranty`;
- `identity.brand`;
- `identity.sku`.

The registry maps aliases/user labels to canonical semantic keys and defines type/freshness expectations. Unknown attributes are stored under valid custom keys, not rejected.

Volatile exact facts remain structured. Long descriptive prose remains in knowledge blocks.

## Decision 12: Simple UX, advanced normalized representation

Workbench/entity API supports simple common fields, arbitrary label/value rows, raw pasted knowledge, optional AI extraction suggestions, and an advanced normalized document view.

AI extraction cannot silently overwrite authoritative user-entered facts. Suggestions require explicit acceptance or remain non-authoritative.

## Decision 13: Evidence system is independent of Agent transcript

`EvidenceCache` keys conceptually include entity ID, selector/topic, and entity revision/freshness bucket.

Application operations:

```text
search_entities(queries, entity_type?)
get_entities(ids, selectors?)
get_evidence(requests[])
```

Application code executes retrieval and cache logic. The model may request evidence through a typed plan, but it cannot invoke arbitrary functions.

Stable facts can be revision-scoped. Volatile facts use TTL or explicit revision/invalidation policy. The planner batches misses and executes independent fetches concurrently.

## Decision 14: Two Agent execution paths

### Factual fast path

Eligible when the selected cluster intent is known, target entity/product confidence is above threshold, the requested fact selector is known, and no comparison/referential reasoning is required.

```text
ClusterEnvelope
 -> deterministic EvidencePlanner
 -> cache/batch evidence
 -> deterministic answer OR one verbalization generation
```

Zero LLM calls are allowed for exact templatable answers.

### Bounded complex path

Used for comparison, ambiguity, pronoun/reference resolution, open-ended usage, or synthesis.

```text
max_planning_generations = 1
max_evidence_rounds = 1 normally
max_evidence_rounds = 2 exceptional configured ceiling
max_final_generations = 1
```

A tool/evidence round is application-executed from a validated typed plan. The Agent cannot create work beyond these limits.

## Decision 15: Structured memory layers

### ScriptState

Authoritative live script position: script set/version, product, sentence index, last completed sentence, and exact next sentence.

### SessionMemory

Bounded structured continuity: introduced products, recent discussed entities, active campaign facts, last spoken topic/product, unresolved commitments.

### TopicMemory

Bounded keyed recent Q&A turns with entity/topic/reference metadata.

### EvidenceCache

Authoritative evidence, independent from conversation turns.

Full transcript persistence is diagnostic/audit data and is not automatically fed back to the LLM. Compaction prefers deterministic structured eviction; no dedicated summarization call is required by default.

## Decision 16: Approved-script sentence map above TextChunker

At binding/start, the runtime deterministically derives sentence spans from exact approved `spoken_text`.

The sentence map contains offsets and exact text slices; it is a runtime derivative, not a new authoring version. The splitter must preserve exact text identity and cannot paraphrase, normalize, or call an LLM.

Each sentence is spoken through the existing canonical verbatim speech path. The full sentence may itself be segmented into multiple phrase-sized TextChunks by Change A.

Therefore:

```text
approved sentence != TextChunk
```

Script cursor advancement occurs only after the sentence-level speech call completes normally.

## Decision 17: Speech Arbiter state machine

Conceptual states:

```text
SCRIPT_READY
SCRIPT_SENTENCE_PLAYING
QNA_PENDING
QNA_PREPARING
QNA_PLAYING
RESUME_BRIDGE
STOPPED/FAILED
```

Priority is `Q&A > SCRIPT_NEXT_SENTENCE`, but an active script sentence is non-preemptible for normal Q&A.

While `SCRIPT_SENTENCE_PLAYING`, reducer processing continues, pending candidates update, stable evidence may be prefetched, and volatile evidence/final expensive generation are deferred until boundary revalidation.

At sentence completion:

```text
revalidate pending Q&A
if none:
    advance/start next sentence
else:
    checkpoint already points to next sentence
    resolve/revalidate evidence
    speak Q&A lead-in + grounded answer
    speak resume bridge when useful
    start exact next approved sentence
```

Emergency/operator hard interrupt remains a separate control-plane operation and may cancel speech immediately.

## Decision 18: Pending Q&A supersession

Only a bounded set of candidates is retained. A newer cluster may replace the pending winner when score/relevance exceeds configured hysteresis.

At the safe boundary the arbiter revalidates cluster activeness, unique-viewer eligibility, cooldown/answer state, product/evidence validity, and current script/session compatibility.

This avoids wasting LLM generation on a temporary winner several seconds before the next safe speech boundary.

## Decision 19: Natural transitions without extra default calls

Lead-in/resume bridges use deterministic Vietnamese templates parameterized by topic, product display name/code, current script product, and optional prior sentence metadata.

The final Q&A generation may include a natural lead-in in the same call. A separate bridge-only LLM call is not part of the normal path.

## Decision 20: Workbench is an executable integration harness

### SE Adapter Simulator

Generates platform-specific simulated upstream events, then displays the normalized canonical request sent to `/events`.

Scenarios cover concurrent sources, burst traffic, retry with same event ID, reordered delivery, batching, malformed events, platform outage, and duplicate viewer activity.

### Entity Data Studio

Simple form + arbitrary fields + raw knowledge + advanced normalized entity view.

### Runtime inspectors

Safety counters; stable clusters/representatives; reconciliation timer/count; score breakdown; exact ClusterEnvelope; memory layers; evidence cache; evidence/tool rounds; current/next script sentence; and arbiter timeline.

## Decision 21: Optional hybrid image-context benchmark

The benchmark is isolated from core correctness.

Always-text control plane includes system/developer/static guardrails, exact IDs, current exact volatile facts, tool schemas, response schema, and current task.

Candidate image knowledge plane may include long static descriptions, shop story/persona background, campaign background, and similar read-only descriptive context.

Acceptance compares identical fixtures in all-text and hybrid modes and records effective/model-reported input tokens, TTFT, total latency, exact number/ID accuracy, Vietnamese diacritics, grounding, tool selection, hallucination, and cost where measurable.

Hybrid mode remains off unless it satisfies configured non-regression thresholds and demonstrates a material token/latency benefit.

## Decision 22: Breaking migration, no dual stack

The following are removed:

- `/ws/platform`;
- `/sessions/{id}/ingest`;
- `/sessions/{id}/chat`;
- public `initial_ingest_mode`;
- platform WebSocket schema generation;
- Workbench clients/fixtures for removed routes;
- synchronous Director ingest path retained solely as fallback for the removed API;
- rigid entity compatibility adapters introduced solely to preserve the replaced schema.

Generated contracts and tests are regenerated in the same change.

## Failure Handling

- Duplicate event: idempotent result; no second semantic effect.
- Safety reject: counted/audited safely; no embedding/Agent effect.
- Embedder failure: follow existing readiness/degraded policy; do not fabricate vectors.
- Reconciliation failure: retain last valid fast-lane cluster state and emit typed diagnostics.
- Evidence fetch failure: deterministic fallback/bounded failure; never invent authoritative facts.
- Volatile evidence stale: refresh/revalidate before speech or state fresh evidence is unavailable.
- Agent/tool budget exceeded: terminate/fallback; do not continue autonomous calls.
- Script sentence speech failure/cancel: do not advance cursor unless normal sentence completion is confirmed.
- Q&A failure: preserve script cursor and resume according to arbiter failure policy.
- Runtime configuration revision: invalidate prepared Agent/Q&A work whose inputs no longer match current revision.

## Observability

Per-session diagnostics SHOULD expose content-safe metadata for event counts, Safety Gate reasons, fast-lane queue/microbatch latency, reconciliation state, cluster IDs/demand, pending Q&A, Agent path, evidence cache, evidence/tool/LLM rounds, token/latency, memory sizes, script sentence index, arbiter state, and Q&A/resume timestamps.

Raw private viewer text MUST NOT be emitted into generic telemetry merely for debugging.

## Security and Trust Boundaries

- SE adapter output is authenticated but viewer payload remains untrusted.
- Viewer text never controls instruction hierarchy.
- Entity repository is authoritative data; Agent output is not.
- Tool plans are schema-validated and allowlisted.
- Tool executor cannot expose arbitrary filesystem, shell, network, or job-management operations.
- Approved script remains immutable.
- Human approval remains authoritative for pre-live scripts.
- Runtime Agent answers do not retroactively become approved script content.

## Migration

1. Introduce new internal models/services and `/events`.
2. Move Workbench simulation to `/events`.
3. Add reducer/ClusterStore behind the new ingress.
4. Migrate Director demand selection to ClusterEnvelope.
5. Migrate universal entity model and consumers.
6. Add evidence + Agent paths.
7. Add sentence cursor/arbiter and runtime binding behavior.
8. Regenerate contracts.
9. Delete removed ingress routes/fallbacks/platform WS schema.
10. Run full contract, reducer, agent, script, Workbench, and OpenSpec validation.

No production-compatible alias is left behind after the migration lands.
