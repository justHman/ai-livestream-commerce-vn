# Design: Approved Script Authoring Pipeline

## Context

The project needs a pre-live authoring plane that can prepare detailed scripts for many products before a runtime livestream starts. Human-written drafts should be cheap: if a draft passes deterministic validation, no LLM call is required. AI must remain optional and explicit. At the same time, AI-generated long-form scripts for a single product may need 10–60 minutes of spoken content, which cannot be safely represented as one unconstrained model response.

The design therefore separates **authoring control flow** from **model generation**. The backend owns a finite workflow, fixed job graph, persistence, retries, and gate/approval state. LLM calls are bounded leaf operations. There is no general agent loop, no arbitrary tool registry, and no model-controlled traversal.

Change A `adaptive-speech-text-chunking` remains the downstream runtime speech primitive, but its final corrected architecture is now an explicit dependency contract rather than an implementation detail. Change B ends at an immutable, human-approved `spoken_text` artifact and a session binding. It does not reimplement chunking, speech-duration estimation, streaming deadlines, TextChunk finality, or TTS logic.

## Goals

- Gate-first, AI-optional authoring.
- Human-final approval of the exact spoken representation.
- Multi-product one-click generation without one giant multi-product response.
- Detailed 10–60 minute scripts without depending on a single output-token budget.
- Predictable semantic model-call counts and bounded retries.
- No autonomous agent loop or free-form tool use.
- Deterministic rules as the authority for pass/fail.
- A project-owned Vietnamese livestream sales-writing skill for generation only.
- Immutable, auditable versions and approval dependencies.
- REST+JSON transactional APIs and SSE progress for long-running generation.
- Authoring can run without creating a live avatar/TTS session.
- Runtime handoff uses only Change A's canonical `backend.application.text_chunker` package contract and the same source-agnostic TextChunker for complete scripts.
- Change B does not duplicate Change A speech-duration estimation, chunk-policy configuration, realtime deadline, or finality ownership.

## Non-goals

- Realtime viewer Q&A generation.
- Autonomous model browsing/research/tool use.
- Automatic approval because a gate or LLM says content is good.
- Automatically retrying gate failures until a model passes.
- Replacing Change A speech segmentation.
- Requiring a neural spelling/toxicity model in the first implementation.
- Fetching third-party `SKILL.md` content at runtime.
- Treating an external toxic-language dataset as a production blacklist without curation.
- Preserving or recreating Change A legacy import paths such as `backend.application.speech_chunking`, sibling `text_chunker.py`, or `render.windows.TextChunk`.
- Adding a script-specific chunker, a full-script bypass that constructs giant `TextChunk` objects, or a source-specific chunker mode.
- Owning `flush_timeout_ms`, streaming deadline scheduling, adaptive `target_chars`, or TextChunk finality inside Change B.

## Decision 0 — Change A final architecture is a hard integration contract

Change B is not allowed to integrate against a transitional Change A shape. Before any Change B implementation begins, Change A must have completed its mandatory architecture correction and benchmark gate. The maintainable upstream shape is:

```text
backend/application/
└── text_chunker/
    ├── __init__.py
    ├── chunker.py
    ├── types.py
    ├── boundaries.py
    ├── duration.py
    └── policy.py
```

The stable import boundary is the package itself, for example:

```python
from backend.application.text_chunker import TextChunker, TextChunk
```

Change B SHALL NOT depend on or recreate any of the following:

```text
backend.application.speech_chunking
backend/application/text_chunker.py   # sibling facade
backend.application.render.windows.TextChunk
compatibility re-exports of TextChunk
source-specific ScriptTextChunker/LLMTextChunker
```

For runtime script speech, the approved full `spoken_text` is simply a complete arbitrary text input to the same source-agnostic TextChunker used by Change A. Change B MUST NOT directly construct a single giant `TextChunk` and MUST NOT bypass the shared segmentation path. Conceptually:

```text
ApprovedScript.spoken_text
        ↓
existing runtime speech ingestion
        ↓
backend.application.text_chunker.TextChunker
        ↓
feed(full_spoken_text) + finalize()
        ↓
TextChunk[]
        ↓
TTS → Avatar
```

The runtime may encapsulate those calls behind an existing speech service; the architectural invariant is that the full-script path reaches the same canonical TextChunker and never has a second segmentation implementation.

Change B also respects these Change A ownership boundaries:

- **realtime timing**: streaming controller/orchestration owns latency deadlines; Change B does not configure or invoke LLM-stream timeout machinery for complete scripts;
- **policy strategy**: fixed and `adaptive_vi` are Change A strategies with typed policy-specific configs; Change B does not pass legacy fixed `target_chars` as an adaptive concept or create a `mode=script`;
- **duration**: Change B reuses the canonical Change A deterministic speech-duration estimator for actual generated text; its own generation-budget calibration only estimates safe model-output work before text exists and is not a second speech-duration implementation;
- **finality**: Change A owns explicit exactly-once TextChunk→AudioWindow→VideoWindow normal completion. Change B supplies text and never reconstructs/stamps final `TextChunk` objects manually.

If Change A's stable package does not expose an interface Change B genuinely needs (for example deterministic speech-duration estimation), Change A's contract must be corrected first. Change B MUST NOT deep-import an internal module or duplicate the implementation to unblock itself.

## Decision 1 — Backend-owned `ScriptSet` aggregate

Authoring is pre-live and therefore SHALL NOT require a runtime `session_id`. The aggregate root is `ScriptSet`:

```text
ScriptSet
├── LiveSessionBrief
├── ordered_product_ids[]
├── ScriptItem(P001)
│   ├── ProductScriptPlan versions
│   ├── Segment versions
│   ├── Compiled ScriptVersion versions
│   └── approved_version_id
├── ScriptItem(P002)
└── ...
```

A ScriptSet is bound to a live session only after required product scripts are fresh and approved.

### Proposed location

```text
services/product/backend_service/
├── resources/
│   └── skills/
│       └── livestream-sales-script/
│           ├── SKILL.md
│           └── references/
│               ├── planning-guidance.md
│               └── spoken-sales-patterns.md
├── src/backend/
│   ├── api/v1/
│   │   └── scripts.py
│   ├── application/
│   │   └── script_authoring/
│   │       ├── __init__.py
│   │       ├── models.py
│   │       ├── service.py
│   │       ├── repositories.py
│   │       ├── approval.py
│   │       ├── session_binding.py
│   │       ├── fingerprints.py
│   │       ├── events.py
│   │       ├── exceptions.py
│   │       ├── gate/
│   │       │   ├── engine.py
│   │       │   ├── registry.py
│   │       │   ├── results.py
│   │       │   └── rules/
│   │       │       ├── format.py
│   │       │       ├── vietnamese.py
│   │       │       ├── profanity.py
│   │       │       ├── style.py
│   │       │       ├── commerce_claims.py
│   │       │       └── tts_readiness.py
│   │       └── generation/
│   │           ├── planner.py
│   │           ├── segment_generator.py
│   │           ├── repair.py
│   │           ├── batch.py
│   │           ├── scheduler.py
│   │           ├── prompt_builder.py
│   │           ├── context_builder.py
│   │           ├── continuity.py
│   │           └── skill_loader.py
│   └── db/sql/
│       └── <existing schema/migration convention>
└── tests/
    ├── unit/script_authoring/
    ├── integration/
    └── contract/
```

The exact SQL migration filename follows the repository's existing SQL convention discovered during implementation; the application boundary and table responsibilities are fixed by this design.

## Decision 2 — Gate-first, AI-optional state machine

```text
EMPTY
  ├─ manual edit ───────────────► DRAFT
  └─ Generate Script ─► PLANNING/GENERATING ─► DRAFT

DRAFT
  └─ Submit ─► GATE_RUNNING
                  ├─ PASS ─► REVIEWABLE
                  └─ FAIL ─► GATE_FAILED
                                ├─ manual edit ─► DRAFT
                                └─ Fix with AI ─► AI_FIXING ─► DRAFT

REVIEWABLE
  └─ Human Approve ─► APPROVED

Any content/dependency-invalidating edit:
REVIEWABLE/APPROVED ─► new DRAFT or STALE
```

Gate PASS never means approved. AI generation/repair never means approved. Only an authenticated human approval command creates an approval record.

## Decision 3 — One canonical `ScriptRuleRegistry`

Rules SHALL be versioned objects with stable IDs and three possible consumers:

```text
Rule
├── id
├── version
├── severity
├── deterministic_check
├── user_message
├── generation_constraint
└── repair_instruction
```

Examples:

```text
FORMAT_*
STYLE_*
VN_SPELLING_*
PROFANITY_*
CLAIM_PRICE_*
CLAIM_DISCOUNT_*
TTS_NUMBER_*
REPETITION_*
SPEECH_DURATION_*
```

The deterministic `ScriptGate` is the pass/fail authority. Prompt builders read rule metadata; they do not maintain a separate hand-copied rules document.

### Rule families

- formatting/schema/Unicode controls;
- Vietnamese punctuation/spacing/spelling heuristics;
- house style, including explicit disallowed punctuation such as em-dash if configured;
- profanity/offensive lexicon and normalized/teencode variants;
- product/promotion/price/discount/factual claim validation;
- TTS readiness for numbers, currency, percent, URL/email/acronym forms;
- local/global repetition and CTA pacing;
- target spoken-duration checks.

Any dataset-derived lexicon requires recorded source, license, retrieval/version identifier, human curation policy, and tests. A span-annotated or classification dataset is training/evaluation evidence, not automatically the runtime blacklist.

## Decision 4 — `display_text` and exact `spoken_text`

A draft/version may carry both:

```text
display_text = "Kem ABC chỉ 299.000đ, giảm 20%."
spoken_text  = "Kem A B C chỉ hai trăm chín mươi chín nghìn đồng, giảm hai mươi phần trăm."
```

Normalization/compilation into `spoken_text` SHALL be deterministic or explicitly reviewed. The human review UI MUST expose the exact `spoken_text` that Change A/VieNeu will receive. Approval hashes bind the spoken artifact, not merely the pretty display text.

## Decision 5 — Different LLM contracts for Generate and Fix

### Generate

Generate is creative and loads:

```text
project-owned LivestreamSalesScriptSkill
+ relevant generation constraints
+ LiveSessionBrief
+ authoritative product facts
+ authoritative promotion facts
+ requested intent/duration
+ ProductScriptPlan/segment assignment
+ compact ContinuityState
```

The project-owned skill is adapted from reviewed copywriting/product-marketing principles such as clarity over cleverness, benefit framing, specificity, customer language, objections, honest claims, and clear calls to action. Third-party skill repositories are reference material only; runtime loads only the pinned project-owned skill.

### Fix

Fix is constrained repair and loads:

```text
original immutable ScriptVersion/SegmentVersion
+ exact failed rule IDs
+ only those rules' repair instructions
+ authoritative facts needed to prevent claim drift
```

Fix SHALL NOT load the sales-copy skill. Its system contract requires minimal edits, preserving compliant wording, meaning, structure, tone, and factual claims unless a failed rule requires a change. The fixed result is a new DRAFT and must be submitted to ScriptGate again.

## Decision 6 — No general agentic loop

The LLM SHALL NOT receive tools that can create jobs, read/write arbitrary files, traverse products, retry itself, or decide how many steps to execute.

Forbidden control flow:

```text
while model_says_continue:
    call_model_or_tool()
```

Allowed control flow is a finite backend state machine with precomputed bounds.

The backend owns:

- product traversal;
- segment count;
- job creation;
- concurrency;
- retries;
- cancellation;
- persistence;
- idempotency;
- gate transitions.

## Decision 7 — Long-form generation = plan + fixed K segments

A 10–60 minute product script SHALL NOT be generated as one model response.

### Planning phase

One bounded planning call produces a structured `ProductScriptPlan`, for example:

```text
1. Hook / problem framing             2 min
2. Product introduction              3 min
3. Feature A -> benefit              4 min
4. Feature B -> benefit              4 min
5. Usage / demonstration guidance    4 min
6. Objection handling                4 min
7. Use cases / customer scenarios    3 min
8. Offer / promotion                 3 min
9. Recap / CTA                       3 min
```

Plan output is schema-validated and may reference only authoritative fact/objection IDs supplied by the backend.

### Fixed segment count

The backend computes a safe generation duration per segment from the configured model capability and calibrated output statistics:

```text
safe_output_tokens = model_max_output_tokens * output_safety_factor
safe_segment_duration_s = calibrated_duration_for(safe_output_tokens)
K = ceil(target_duration_s / safe_segment_duration_s)
```

The first implementation may use conservative configuration until empirical calibration exists, but it MUST compute and persist `K` before segment generation starts. This pre-generation calculation is a **GenerationBudgetCalibration** concern (model max output, safety factor, observed model-output characteristics). It is not the canonical speech-duration estimator.

After prose exists, target/actual spoken-duration checks SHALL use Change A's canonical deterministic speech-duration estimation interface from `backend.application.text_chunker`. Change B MUST NOT implement a second Vietnamese speech-duration estimator in `script_authoring`.

`K` is a hard workflow bound. The model cannot increase it.

### Call budget

For one product under normal semantic execution:

```text
1 planning call + K segment calls
```

Explicit human actions such as **Regenerate Segment** or **Fix with AI** create additional separately recorded calls. Provider transport retries are separately bounded and do not permit model-controlled semantic expansion.

## Decision 8 — Continuity without extra summary calls

Segments for one product are generated sequentially. Each segment generation receives a compact backend-owned `ContinuityState`:

```text
previous_segment_tail
covered_fact_ids
handled_objection_ids
cta_count
used_opening_fingerprints
last_topic
next_topic
```

The segment result returns structured continuity metadata alongside `display_text`/`spoken_text`. The backend validates referenced IDs against authoritative plan/fact IDs before incorporating them into subsequent state.

No additional LLM summarization call is required between segments.

## Decision 9 — Segment Gate then Full Script Gate

Immediately after each generated segment:

```text
GENERATE segment N
  -> validate schema
  -> Segment Gate
      PASS -> persist and advance to N+1
      FAIL -> stop this product workflow at N
```

A segment gate failure SHALL NOT trigger automatic AI repair or automatic regeneration and SHALL NOT spend calls on later segments. Human action is required to manually edit, regenerate that segment, or invoke Fix with AI.

After all segments pass, the backend compiles the exact selected segment versions and runs Full Script Gate for:

- cross-segment repetition;
- duplicated or conflicting claims;
- required fact/topic coverage;
- CTA frequency/pacing;
- tone/persona drift;
- global spoken duration;
- transition coherence.

Only a full PASS becomes `REVIEWABLE`.

## Decision 10 — Multi-product batch = batch UX, per-product workflows

`Generate All` is one user action but not one model response.

```text
Batch
├── Product A: plan -> A1 -> A2 -> ... -> AK
├── Product B: plan -> B1 -> B2 -> ... -> BK
└── Product C: plan -> C1 -> C2 -> ... -> CK
```

Different products MAY progress concurrently up to backend `max_product_concurrency`. Segments within the same product remain sequential by default for continuity.

A product failure does not fail completed sibling products. Batch state exposes per-product status and counts.

## Decision 11 — Deterministic cost/call preview

Before generation, the API SHALL support a no-LLM preview based on selected product durations/model policy:

```json
{
  "products": [
    {"product_id": "P001", "target_duration_s": 600, "planned_segment_count": 3, "estimated_semantic_calls": 4},
    {"product_id": "P002", "target_duration_s": 3600, "planned_segment_count": 15, "estimated_semantic_calls": 16}
  ],
  "estimated_semantic_calls_total": 20
}
```

The estimate is a semantic-call budget for the planned workflow and excludes explicit future user actions. The pre-generation calculation uses Change B's `GenerationBudgetCalibration` (provider output limits and observed model-output statistics), not a second speech-duration estimator. Once text exists, actual spoken-duration checks use Change A's canonical deterministic estimator. UI wording must make that distinction clear.

## Decision 12 — Bounded retries and idempotency

### Transport/provider failure

Infrastructure retry MAY occur with a configured finite `max_attempts`, using the same immutable generation input and idempotency/job identity where provider semantics permit.

### Content/gate failure

No automatic semantic retry. The workflow becomes `GATE_FAILED`/`FAILED_CONTENT` and awaits a human command.

### API idempotency

Long-running create commands use an idempotency key derived from/requested with:

```text
script_set_id
script_set_revision
selected product IDs
requested duration/config
model/skill/rules fingerprint
client Idempotency-Key
```

A repeated equivalent request while an existing workflow is queued/running returns the existing workflow rather than double-spending model calls.

## Decision 13 — Immutable versions and fingerprints

Manual edits, AI generation, AI repair, and regeneration create new immutable versions.

Suggested entities:

```text
script_sets
script_items
product_script_plans
script_segments
script_versions
script_gate_runs
script_approvals
script_generation_batches
script_generation_jobs
```

`GenerationFingerprint` records enough reproducibility metadata without chain-of-thought:

```text
model/provider identifier
skill version/hash
rule-set version/hash
prompt-template version
product-facts version
promotion version
persona/brief version
generation parameters
plan version
```

## Decision 14 — Approval is human-only and dependency-bound

Approval requires:

- exact current compiled `ScriptVersion`;
- successful latest Full Script Gate for that exact version;
- no stale fact/promotion/persona/rule dependencies;
- authenticated human action.

Conceptual approval hash:

```text
SHA256(
    compiled_spoken_text
  + ordered_segment_version_hashes
  + plan_version_hash
  + rule_set_version
  + product_facts_version
  + promotion_version
  + persona_brief_version
)
```

If any bound dependency changes, the approved item becomes `STALE`/not runtime-eligible until resubmitted/reviewed.

## Decision 15 — Transition policy

`LiveSessionBrief` SHALL distinguish:

```text
ORDER_AWARE
ORDER_AGNOSTIC
```

- `ORDER_AWARE` may generate explicit transitions using previous/next product summaries when product order is locked.
- `ORDER_AGNOSTIC` generates a standalone product core with generic entry/exit language so the Director may reorder products at runtime.

The core sales content must remain usable independently of a baked transition.

## Decision 16 — REST/JSON commands + SSE progress

Authoring API root:

```text
/api/v1/script-sets
```

Authoring does not reuse the runtime avatar/session WebSocket. CRUD/commands are REST+JSON. Long-running generation returns `202 Accepted`. Batch/job progress is one-way server-to-browser SSE.

### Core endpoints

```text
POST   /api/v1/script-sets
GET    /api/v1/script-sets/{set_id}
PATCH  /api/v1/script-sets/{set_id}

PUT    /api/v1/script-sets/{set_id}/products/{product_id}/draft
POST   /api/v1/script-sets/{set_id}/products/{product_id}/submit
POST   /api/v1/script-sets/{set_id}/products/{product_id}/generation-preview
POST   /api/v1/script-sets/{set_id}/products/{product_id}/generate
POST   /api/v1/script-sets/{set_id}/products/{product_id}/segments/{segment_index}/regenerate
POST   /api/v1/script-sets/{set_id}/products/{product_id}/fix
POST   /api/v1/script-sets/{set_id}/products/{product_id}/approve

POST   /api/v1/script-sets/{set_id}/generation-preview
POST   /api/v1/script-sets/{set_id}/generate-batch
POST   /api/v1/script-sets/{set_id}/approve-batch

GET    /api/v1/script-sets/{set_id}/generation-batches/{batch_id}
GET    /api/v1/script-sets/{set_id}/generation-batches/{batch_id}/events
POST   /api/v1/script-sets/{set_id}/generation-batches/{batch_id}/cancel

PUT    /api/v1/sessions/{session_id}/script-set
```

### HTTP/domain semantics

- malformed request/body → normal 4xx (`400/404/409/422` as appropriate);
- deterministic ScriptGate completed with violations → HTTP `200`, domain state `gate_failed`;
- accepted async generation/fix/regeneration → `202`;
- invalid state transition such as AI Fix on a non-failed version → `409`;
- stale/not-ready ScriptSet binding → `409` with structured missing/stale details.

### SSE event examples

```text
event: product.planning_started
event: product.plan_ready
event: segment.started
event: segment.gate_passed
event: segment.gate_failed
event: product.reviewable
event: product.failed
event: batch.progress
event: batch.completed
event: batch.cancelled
```

Every event carries stable IDs (`script_set_id`, `batch_id`, `product_id`, optional `segment_index`) and monotonic sequence/revision information sufficient for client deduplication/recovery.

## Decision 17 — Runtime session binding uses the canonical Change A path

The session binding command verifies:

- ScriptSet exists;
- requested/required product scripts are approved;
- approvals are fresh;
- ScriptSet products/order policy are compatible with the runtime plan/catalog.

At runtime, the backend loads the exact approved `spoken_text`. The complete script MUST enter the same source-agnostic Change A TextChunker path that accepts arbitrary LLM fragments and full text. No HTTP hop is introduced between Change B and Change A, and no script-specific chunking path is created.

```text
ApprovedScript.spoken_text
        ↓
canonical runtime speech ingestion
        ↓
backend.application.text_chunker.TextChunker
        ↓
content chunks
        ↓
VieNeu
        ↓
Avatar
```

The Change B integration layer SHALL NOT:

- import `backend.application.speech_chunking`;
- import or re-export `TextChunk` from `backend.application.render.windows`;
- construct `TextChunk(...)` directly to represent the whole approved script;
- create `ScriptTextChunker`, `VerbatimChunker`, or source-specific chunker modes;
- own `check_timeout`, `flush_timeout_ms`, streaming latency deadlines, or fixed/adaptive chunk sizing defaults;
- stamp or reconstruct `is_final` on TextChunk objects.

A complete approved script has all text available immediately. Therefore the authoring/runtime binding path does not need streaming-deadline logic. It simply supplies the exact approved text to the canonical content-segmentation path and allows Change A to own policy selection, chunk creation, and finality semantics.

## Decision 18 — Skill provenance and project ownership

The initial `livestream-sales-script` skill SHOULD adapt reviewed principles from external references such as `coreyhaines31/marketingskills` `copywriting` and `product-marketing`, including clarity, benefit framing, specificity, customer language, objections, honest claims, and CTA discipline. It MUST be rewritten for Vietnamese spoken livestream selling, long-form planning, segment continuity, TTS suitability, factual constraints, and the project's `ScriptIntent`/rule system.

Runtime MUST load the project-owned file from the repository/package. It MUST NOT fetch a mutable remote skill during a generation request.

The skill SHALL include at least two operation sections:

- `PLAN_PRODUCT_SCRIPT`: build a non-repetitive 10–60 minute content architecture with topic/fact/objection/CTA distribution;
- `GENERATE_SCRIPT_SEGMENT`: write only the assigned segment, respect continuity/remaining coverage, use natural spoken Vietnamese, and avoid prematurely recapping the whole product.

Repair remains outside this skill.

## Decision 19 — Moderation/profanity data is curated policy input

The first implementation may combine exact/normalized lexicon, teencode/obfuscation patterns, deterministic Unicode normalization, and optionally a separately evaluated classifier. External corpora such as Vietnamese offensive/hate-speech datasets can inform lexicon candidates and test fixtures, but production policy requires:

- license/provenance record;
- manual curation/versioning;
- false-positive tests, including product/brand allowlists;
- explicit severity/action semantics.

No network lookup is required during ScriptGate.

## Decision 20 — Persistence/recovery is workflow-critical

Generation state SHALL survive API worker restart. A persisted job records immutable input fingerprint, planned segment count, current segment index, attempt counts, status, and generated version references. On recovery, the backend resumes only from a persisted finite next step; it does not reconstruct control flow from model prose.

Cancellation stops scheduling new semantic calls, preserves completed immutable artifacts, marks active/pending jobs consistently, and emits a terminal SSE event.

## Decision 21 — Observability and privacy

Record at least:

- requested target duration;
- planned K;
- estimated and actual semantic calls;
- provider transport attempts;
- per-segment generation latency/output token usage when available;
- gate pass/fail by rule ID;
- batch/product/segment lifecycle durations;
- approval/staleness transitions.

Raw script content SHALL NOT be logged by default. Generation prompts/outputs are persisted only in the explicit script-version store required for the feature, under existing authorization/data-retention policy.

## Testing strategy

### Unit

- state transitions and invalid transitions;
- rule registry and each deterministic rule family;
- display→spoken normalization;
- generation/fix prompt separation;
- planner schema and K calculation;
- continuity-state validation;
- segment/full gate semantics;
- immutable hashes/fingerprints;
- idempotency/call-budget calculation.

### Integration

- SQL repository/version lifecycle;
- gate-first manual draft path with zero LLM calls;
- one product long-form plan + K segments;
- segment fail stops later calls;
- multi-product bounded concurrency;
- restart/recovery/cancel;
- REST and SSE contract;
- session binding rejects stale/unapproved scripts.

### Contract

- no model-controlled tools/job creation;
- no automatic content-failure repair loop;
- human approval required;
- exact `spoken_text` passed to Change A;
- full approved script uses the same canonical `backend.application.text_chunker.TextChunker` path and is not wrapped in a manually constructed giant TextChunk;
- no Change B production/test imports of `backend.application.speech_chunking` or `render.windows.TextChunk`;
- no Change B-owned `flush_timeout_ms`, `check_timeout`, adaptive `target_chars`, source-specific chunker mode, duplicate speech-duration estimator, or manual TextChunk finality stamping;
- Change B remains blocked unless Change A architecture-correction evidence and benchmark PASS evidence both exist.

## Rollout

1. Verify Change A final-package architecture, canonical TextChunk cleanup, full-script same-chunker path, finality/config ownership, strict validation, and VieNeu benchmark PASS evidence before Change B code changes.
2. Record the stable Change A package exports Change B may consume; if the needed duration estimator is not public, correct Change A first rather than deep-importing.
3. Land domain/state/repository and deterministic ScriptGate before any LLM generation UI.
4. Land manual gate→review→approve→bind path and prove it works with zero LLM calls.
5. Add Generate/Fix single-product operations.
6. Add long-form planner/segment workflow and recovery.
7. Add multi-product batch/SSE and Workbench UX.
8. Add curated Vietnamese moderation resources and extended rules.
9. Run full regression, architecture-cleanliness audit, and local approved-script → canonical Change A TextChunker → VieNeu playback smoke before declaring the capability ready.

## External design references (non-runtime dependencies)

The project-owned skill should be authored from reviewed principles rather than vendoring a mutable upstream prompt unchanged. Reference sources reviewed during proposal:

- `coreyhaines31/marketingskills` — `skills/copywriting/SKILL.md`: https://github.com/coreyhaines31/marketingskills/blob/main/skills/copywriting/SKILL.md
- `coreyhaines31/marketingskills` — `skills/product-marketing/SKILL.md`: https://github.com/coreyhaines31/marketingskills/blob/main/skills/product-marketing/SKILL.md

These references do not authorize runtime network fetching. Implementation task 5.x creates and versions the project-specific `livestream-sales-script` skill inside `backend_service/resources/skills/`.
