## Why

The live runtime now has two speech sources that must cooperate during one commerce livestream:

1. immutable human-approved product scripts from `approved-script-authoring-pipeline`; and
2. reactive viewer Q&A selected from concurrent TikTok, Shopee, Facebook, YouTube, and future platform traffic.

The current runtime was built incrementally and exposes overlapping viewer-ingress and scheduling paths. Viewer comments may enter through `/ws/platform/{session_id}`, `/sessions/{session_id}/ingest`, or `/sessions/{session_id}/chat`; `/ingest` still retains a synchronous Director fallback; the Coordinator polls on a fixed tick and reconstructs semantic clusters from a rolling comment history; routing commits a single product before clustering; demand scoring is primarily message-count based; and the current approved-script handoff speaks a whole product `spoken_text` without a sentence-addressable runtime cursor.

That architecture is not sufficient for the target behavior:

- all platforms must normalize through one stable SE→AI contract;
- retries/duplicates must be idempotent;
- unsafe or prompt-injection viewer content must be rejected before embedding;
- clustering must reduce traffic before any LLM/Agent sees it;
- demand must be based on stable semantic clusters and unique viewers, not raw repeated messages;
- product/shop knowledge must support arbitrary commerce verticals without introducing a new rigid schema for every category;
- the Agent must use bounded structured memory and authoritative evidence retrieval instead of an ever-growing chat transcript;
- approved script speech must continue exactly after reactive Q&A;
- Q&A may preempt the *next* script sentence but MUST NOT cut an already-started approved sentence in the middle;
- tool and LLM rounds must remain code-bounded and observable.

The change therefore creates one canonical multi-platform event ingress, a persistent bounded live-demand reducer, a universal entity/evidence model, a bounded Agentic Director, and a sentence-level speech arbiter above the existing canonical `TextChunker`.

The change intentionally removes replaced runtime contracts instead of preserving compatibility aliases. The SE integration boundary and the AI runtime must each have one source of truth.

## What Changes

- Add one canonical `POST /api/v1/sessions/{session_id}/events` endpoint accepting one or many normalized `PlatformEvent` values in the same contract.
- Remove `/api/v1/ws/platform/{session_id}`, `/api/v1/sessions/{session_id}/ingest`, and `/api/v1/sessions/{session_id}/chat`; remove their client/test/contract artifacts and the synchronous Director ingest fallback used only by the removed API.
- Define a stable event envelope with `event_id`, platform provenance, source stream identity, occurrence timestamp, canonical event type, normalized viewer identity when applicable, and typed payload.
- Treat `event_id` as an idempotency key so SE retries do not inflate persistence, embeddings, clusters, demand, or pivot signals.
- Add a deterministic pre-embedding Safety Gate covering malformed content, replay/duplicate flood, spam, curated toxicity/profanity/harassment, configured unsafe sexual content, and prompt-injection attempts targeting the downstream Agent/LLM.
- Replace unconditional 300 ms polling semantics with an event-driven fast lane whose configurable microbatch wait is only a maximum coalescing delay.
- Preserve a configurable rolling demand horizon, initially 75 seconds, as a relevance policy distinct from batching.
- Add heavier cluster reconciliation when either 100 accepted comments accumulate or 60 seconds elapse since the first unreconciled comment, whichever occurs first.
- Replace ephemeral full-window cluster reconstruction with a bounded stable `ClusterStore` that keeps stable cluster IDs, centroids/medoids, representatives, unique-viewer counts, product candidates, cohesion, novelty, and lifecycle state.
- Make pre-cluster product/intent routing soft evidence rather than an authoritative hard partition; allow cluster-level correction and explicit multi-product clusters.
- Rank actionable demand using unique viewers, message demand, intent/actionability, recency, current-script/product relevance, product-resolution confidence, novelty, and configured commerce urgency.
- Introduce a compact `ClusterEnvelope` as the only normal viewer-demand boundary into the Agentic Director; the Agent MUST NOT receive the uncontrolled rolling raw comment window.
- Add bounded `ScriptState`, `SessionMemory`, `TopicMemory`, and `EvidenceCache`; full transcripts may be persisted for audit but SHALL NOT automatically become future model context.
- Add cache-aware batch evidence retrieval with generic entity search/get-evidence operations and freshness semantics for volatile facts.
- Add deterministic factual fast paths and a bounded complex-agent path; normal complex execution targets at most one planning generation, one batch evidence round, and one final answer generation, with any second evidence round exceptional and explicitly budgeted.
- Replace rigid product/shop runtime assumptions with a universal entity document model: small core envelope + typed flexible facts + knowledge blocks + relations + revisions.
- Add a Common Fact Registry for code-relevant concepts while allowing arbitrary domain-specific `custom.*` facts without source-code changes.
- Migrate affected API, Director, script-authoring context, persistence, Workbench types, fixtures, diagnostics, and tests to the universal entity model; do not add a legacy adapter path for the replaced rigid schema.
- Extend Workbench with an SE Platform Adapter Simulator, Shop/Product Data Studio, stable cluster/ranking inspector, Agent context/evidence inspector, script cursor inspector, and speech-arbiter timeline.
- Preserve immutable approved `spoken_text` and human approval semantics from Change B.
- Derive a deterministic sentence map/cursor from the exact approved `spoken_text`; do not rewrite approved content.
- Add a sentence-level Speech Arbiter above `TextChunker`: once a script sentence starts, it owns speech until sentence completion; high-priority Q&A may take the next boundary, then the script resumes at the exact next approved sentence.
- Do not treat `TextChunk` as a sentence. Change A continues to own phrase segmentation, runtime chunking policy, streaming deadlines, and exactly-once finality.
- Add natural deterministic or same-generation Q&A lead-in/resume transitions without introducing a dedicated extra LLM call by default.
- Allow stable evidence prefetch for high-confidence pending Q&A while a sentence is playing; volatile price/stock/promotion/availability evidence is refreshed or revalidated near the speech boundary.
- Add an optional benchmark-only hybrid text+image context-compression experiment for vision-capable models. System/developer instructions, exact IDs, tool schemas, dynamic exact facts, and response schemas remain text control-plane context.
- Add deterministic benchmarks for idempotency, safety-before-embedding, fast-lane latency, reconciliation, cluster stability, unique-viewer demand, multi-product resolution, bounded memory, evidence-cache freshness, tool/LLM budgets, pronoun follow-up resolution, and exact script Q&A resume.

## Capabilities

### New Capabilities

- `multi-platform-event-ingress`: one normalized multi-platform SE→AI event contract with idempotency, safety gating, provenance, and Workbench simulation.
- `persistent-live-demand-reducer`: event-driven fast clustering, rolling demand, reconciliation, stable clusters, unique-viewer-aware ranking, multi-product resolution, and `ClusterEnvelope` output.
- `universal-commerce-entity-context`: cross-domain shop/product/entity documents, flexible facts, knowledge blocks, relations, revisions, retrieval, and context rendering.
- `agentic-live-director`: bounded evidence-aware viewer Q&A with structured memory, Evidence Planner/cache, batch tools, factual fast path, and controlled complex-agent path.
- `script-qna-speech-arbitration`: sentence-level approved-script cursor, pending-Q&A arbitration, natural transitions, and exact script resume.
- `agent-context-compression-benchmark`: optional all-text versus hybrid text+image benchmark with correctness, token, and latency acceptance gates.

### Modified Capabilities

- `commerce-comment-clustering`: replace ephemeral hard-partitioned clusters and raw-message demand with persistent incremental/reconciled demand clusters, unique-viewer ranking, stable topic identity, and soft/multi-product resolution.
- `approved-script-authoring-pipeline`: preserve authoring/approval semantics while extending runtime consumption to sentence-addressable immutable approved content.
- `director-diagnostics`: expose event, safety, reducer, cluster, memory, evidence/tool, script cursor, and arbitration state.
- `editable-session-configuration`: replace obsolete ingest-mode configuration with fast-lane, rolling-horizon, reconciliation, memory/tool-budget, and arbitration controls.

## Dependency and Sequencing

This change depends on the already-landed final contracts from:

1. `adaptive-speech-text-chunking`;
2. `approved-script-authoring-pipeline`;
3. the current multi-session TTS/runtime contracts.

Change A remains the sole owner of:

- canonical `backend.application.text_chunker`;
- phrase-sized `TextChunk` creation;
- fixed/adaptive chunk policy;
- streaming deadlines;
- TTS/playback runtime hints;
- TextChunk→AudioWindow→VideoWindow finality.

The new sentence cursor and Speech Arbiter sit above `TextChunker` and MUST NOT create a script-specific chunker, stamp finality, or infer sentence completion from `TextChunk` identity.

Change B remains the sole owner of:

- pre-live authoring;
- ScriptGate;
- immutable versions;
- human approval;
- approved `spoken_text`.

The live Agentic Director may answer viewers during approved-script playback but MUST NOT mutate or rewrite approved script artifacts.

Recommended internal implementation order:

1. canonical `/events` contract, idempotent ingestion, Safety Gate, and SE simulator;
2. bounded state, persistent reducer, reconciliation, and ranking migration;
3. universal entity model and repository/API/Workbench migration;
4. Evidence Planner, EvidenceCache, and batch retrieval;
5. bounded Agentic Director;
6. approved-script sentence cursor and Speech Arbiter;
7. transition speech and end-to-end Q&A/resume behavior;
8. diagnostics and Workbench inspectors;
9. optional context-compression benchmark;
10. delete replaced ingress/model compatibility paths and regenerate contracts.

Later agent/model behavior MUST NOT be used to hide correctness bugs in deterministic ingestion, clustering, evidence, or arbitration layers.

## Impact

### Backend API and contracts

- add `POST /api/v1/sessions/{session_id}/events`;
- remove `/api/v1/ws/platform/{session_id}`;
- remove `/api/v1/sessions/{session_id}/ingest`;
- remove `/api/v1/sessions/{session_id}/chat`;
- retain `/api/v1/ws/control/{session_id}`;
- regenerate backend OpenAPI;
- remove platform WebSocket schema generation/artifact;
- update Workbench clients and contract tests atomically.

### Backend application

Expected new/refactored responsibility boundaries:

```text
backend/application/
├── platform_events/
├── director/
│   ├── reducer.py
│   ├── cluster_store.py
│   └── reconciliation.py
├── entity_catalog/
├── agentic_director/
└── live_runtime/
    ├── script_cursor.py
    ├── pending_qa.py
    ├── speech_arbiter.py
    └── transitions.py
```

Exact filenames may change in `design.md`, but ingestion, demand reduction, evidence/agent reasoning, script runtime state, and speech arbitration MUST remain independently testable rather than being added to one Coordinator monolith.

### LLM integration

- extend the model-agnostic request/result seam only as required for typed tool-aware execution and optional multimodal context;
- preserve existing normal generation consumers;
- keep tool execution application-owned;
- forbid arbitrary filesystem/web/job-management tools;
- expose token/latency/tool-round telemetry.

### Persistence

Persist or recover where required:

- event idempotency identities;
- normalized viewer references;
- stable cluster state or sufficient recovery state;
- bounded session/topic memory;
- entity documents and revisions;
- evidence freshness metadata;
- script cursor/checkpoints;
- Q&A/arbitration diagnostics.

High-rate ephemeral updates MUST remain bounded and MUST NOT become unnecessary durable writes without a recovery requirement.

### Workbench

Add:

- multi-platform SE Adapter Simulator;
- retry/duplicate/out-of-order/malformed/burst scenarios;
- canonical event request preview;
- universal Shop/Product Data Studio;
- stable cluster inspector;
- Agent context/evidence/tool inspector;
- script sentence cursor;
- speech timeline/arbiter view.

Workbench becomes the executable integration reference for the future SE Platform Adapter Router.

### Tests and benchmarks

Add deterministic coverage for:

- simultaneous multi-platform event ingestion;
- event-id retry/idempotency;
- Safety Gate before embedding;
- fast-lane responsiveness;
- rolling-horizon expiry;
- 100-comments-or-60-seconds reconciliation;
- stable cluster identity and arrival-order sensitivity;
- cross-product and multi-product questions;
- unique-viewer versus repeated-message demand;
- product ambiguity confidence;
- representative quality;
- bounded memory over long synthetic livestreams;
- EvidenceCache freshness and invalidation;
- batch evidence fan-in;
- maximum LLM/tool rounds;
- TopicMemory follow-ups;
- script playback while another-product Q&A wins;
- no mid-sentence preemption;
- exact next-sentence resume;
- approved-script immutability;
- stale volatile evidence rejection/refresh;
- Workbench SE contract replay;
- optional all-text versus hybrid image-context benchmark.

### Breaking migration

This is intentionally a breaking runtime contract change.

The AI backend SHALL NOT expose a dual-stack compatibility period for the removed viewer-ingress routes or rigid product/shop contract. SE integrates against the new event/entity contracts only.

### Deployment

No AWS topology, Terraform stage, GPU tier, or production-release automation change is required merely to implement this runtime architecture. Existing teardown-first and cost-control rules remain unchanged.

## Out of Scope

- implementing TikTok/Shopee/Facebook/YouTube APIs inside the AI backend;
- replacing the SE Platform Adapter Router;
- open-web autonomous browsing by the live Agent;
- Agent mutation of authoritative product/shop data;
- Q&A rewriting approved scripts;
- mid-sentence approved-script interruption;
- unbounded agent/tool loops;
- a second script-specific TextChunker;
- mandatory new NoSQL infrastructure without design/benchmark justification;
- making image-context compression the default before benchmark acceptance.
