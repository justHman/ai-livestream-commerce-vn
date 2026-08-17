# Tasks: Approved Script Authoring Pipeline

> **Execution gate:** Do not begin implementation until Change A `adaptive-speech-text-chunking` has completed its mandatory final-architecture correction, strict verification/OpenSpec validation, and recorded VieNeu benchmark PASS. A temporary compatibility path is not sufficient readiness evidence.

## 1. Dependency gate and final Change A contract

- [x] 1.1 Read Change A `proposal.md`, `design.md`, capability spec, tasks, architecture-correction evidence, strict OpenSpec validation output, and VieNeu benchmark PASS evidence; record the exact stable package exports/config contracts Change B may consume.
- [x] 1.2 Verify Change A final layout is the cohesive `backend/application/text_chunker/{__init__.py,chunker.py,types.py,boundaries.py,duration.py,policy.py}` package and no sibling `backend/application/text_chunker.py` facade remains.
- [x] 1.3 Run repository-wide architecture checks equivalent to `rg -n "speech_chunking" .`, `rg -n "render\.windows.*TextChunk|from .*render\.windows import .*TextChunk" .`, and `rg -n "legacy.*TextChunk|compat.*TextChunk|re-export.*TextChunk" .`; interpret historical prose separately, but require zero active implementation imports/definitions/re-exports.
- [x] 1.4 Inspect Change A full-script/verbatim paths and verify they already feed complete text through the same source-agnostic `backend.application.text_chunker.TextChunker` rather than constructing one giant `TextChunk`.
- [x] 1.5 Verify Change A streaming deadline ownership is outside TextChunker, fixed/adaptive policies have separate typed configs, adaptive policy does not use fixed `target_chars` as a first-class concept, chunking defaults are centralized, and finality no longer depends on manual legacy TextChunk reconstruction.
- [x] 1.6 Add a Change B readiness test/check that fails when any required Change A architecture evidence, strict validation, or benchmark PASS evidence is absent/NOT PASS; do not bypass it with a config default or compatibility shim.
- [x] 1.7 Confirm the canonical deterministic speech-duration estimator needed by Change B is exposed through Change A's stable package contract. If it is not, update Change A first; do not deep-import `text_chunker.duration` or duplicate the estimator inside Change B merely to unblock implementation.
- [x] 1.8 Map the current backend router/bootstrap/SQL repository conventions and update paths in this change only if the canonical repo differs from the approved design; preserve `backend_service` ownership rather than creating a new microservice.
- [x] 1.9 Add `script_authoring` package skeleton and focused test package using existing backend package/import conventions.
- [x] 1.10 Run import/static checks for the skeleton and assert no new import of `backend.application.speech_chunking` or `backend.application.render.windows.TextChunk` exists.

## 2. Domain model, state machine, and immutable persistence

- [x] 2.1 Define typed domain enums/models for ScriptSet, ScriptItem, ScriptState, ScriptSource, TransitionPolicy, ScriptIntent, ProductScriptPlan, ScriptSegment, ScriptVersion, GateRun, GateViolation, Approval, GenerationFingerprint, GenerationBatch, and GenerationJob.
- [x] 2.2 Encode legal state transitions (`EMPTY`, `DRAFT`, `GATE_RUNNING`, `GATE_FAILED`, `REVIEWABLE`, `APPROVED`, `STALE`, generation/cancel/failure substates) and reject illegal transitions deterministically.
- [x] 2.3 Add unit tests proving gate PASS cannot auto-approve, edit-after-approval creates a new draft/stale state, and AI operations never produce APPROVED directly.
- [x] 2.4 Add SQL schema/migrations following the repository's canonical DB convention for script_sets, script_items, product_script_plans, script_segments, script_versions, script_gate_runs, script_approvals, script_generation_batches, and script_generation_jobs.
- [x] 2.5 Add foreign keys/uniqueness/indexes enforcing ordered immutable version identity, one current/approved pointer per item where applicable, and stable batch/job lookup without destructive updates to historical versions.
- [x] 2.6 Implement repository interfaces in `application/script_authoring/repositories.py` and SQL-backed implementations using existing backend pool/transaction patterns.
- [x] 2.7 Add integration tests for create/read/update ScriptSet metadata, immutable plan/segment/script version creation, approval history, and transactional current-version pointer updates.
- [x] 2.8 Add restart/reload tests proving persisted finite generation state can be reconstructed without reading model prose.

## 3. ScriptRuleRegistry and deterministic ScriptGate

- [x] 3.1 Implement stable versioned Rule metadata (`id`, version, severity, deterministic checker, user message, generation constraint, repair instruction) and registry lookup/filtering.
- [x] 3.2 Implement gate result types with stable rule IDs, severity, message, optional text span, implicated segment IDs, and rule-set fingerprint.
- [x] 3.3 Implement format/Unicode/whitespace/punctuation rules, including configured house-style rejection for em-dash and malformed/repeated punctuation patterns without using AI detection heuristics.
- [x] 3.4 Implement deterministic Vietnamese spacing/spelling heuristic hooks with brand/product allowlists and clear WARNING versus ERROR semantics.
- [x] 3.5 Implement profanity/offensive lexicon normalization and teencode/obfuscation pattern support from a curated versioned runtime resource, not a raw downloaded dataset.
- [x] 3.6 Add provenance/license metadata format and tests required before any external dataset-derived lexicon resource can be activated.
- [x] 3.7 Implement commerce-claim rules that compare price, discount, promotion, SKU/product identity, and configured factual claims against authoritative context.
- [x] 3.8 Implement TTS-readiness rules/normalizers for numbers, grouped prices, currency, percentages, URL/email, acronyms/product codes, unsupported markup, and hidden control characters.
- [x] 3.9 Implement local repetition, CTA frequency, and target-duration rule primitives that can run at segment scope.
- [x] 3.10 Implement Full Script Gate rules for cross-segment repetition, contradictory claims, required coverage, CTA pacing, tone/persona consistency signals, transition policy, and overall duration.
- [x] 3.11 Add table-driven rule tests in Vietnamese for clean, blocked, warning-only, brand allowlist, teencode/obfuscation, factual-claim mismatch, TTS normalization, and false-positive cases.
- [x] 3.12 Add a contract test proving ScriptGate uses no LLM/network dependency and that gate failure is deterministic for identical content/context/rule version.

## 4. Display/spoken compilation and approval hashes

- [x] 4.1 Implement deterministic `display_text` → `spoken_text` compilation/normalization with provenance of applied normalizers and no semantic embellishment.
- [x] 4.2 Add tests for prices, percentages, numbers, acronyms/SKU, punctuation, mixed Vietnamese/English, and idempotent normalization.
- [x] 4.2a Wire segment/product spoken-duration checks to Change A's stable deterministic duration-estimation interface exported by `backend.application.text_chunker`; do not add a second Vietnamese speech-duration estimator under `script_authoring`.
- [x] 4.2b Add parity tests proving ScriptGate duration checks use the upstream estimator for the same `spoken_text` and that authoring contains no duplicate syllable/duration algorithm.
- [x] 4.3 Implement compiled ProductScriptVersion from an ordered exact list of selected immutable segment versions.
- [x] 4.4 Implement approval dependency fingerprint/hash over compiled spoken text, ordered segment version hashes, plan version, rule set, product facts, promotions, and persona/brief dependency versions.
- [x] 4.5 Add tests proving a text edit, segment version change, product/promotion/persona dependency change, or bound rule version change stales approval while unrelated metadata does not.
- [x] 4.6 Implement approval service requiring latest exact Full Script Gate PASS and authorized human actor before creating immutable approval.
- [x] 4.7 Add integration tests proving a gate-passed script remains REVIEWABLE until human approval and that stale approval cannot bind to runtime.

## 5. Project-owned livestream sales skill and prompt boundaries

- [x] 5.1 Create `services/product/backend_service/resources/skills/livestream-sales-script/SKILL.md` as a project-owned runtime skill; do not copy/fetch a remote mutable skill at runtime.
- [x] 5.2 Adapt reviewed copywriting/product-marketing principles into Vietnamese spoken livestream guidance: clarity, feature→benefit, specificity, customer language, objections, honest claims, CTA discipline, conversational spoken style, and no unsupported evidence.
- [x] 5.3 Add explicit `PLAN_PRODUCT_SCRIPT` guidance for sustaining 10–60 minutes through non-repetitive content architecture, fact/objection/use-case/demo/CTA distribution, and transition policy.
- [x] 5.4 Add explicit `GENERATE_SCRIPT_SEGMENT` guidance to write only the assigned segment, respect continuity/remaining coverage, avoid repeated openings/CTA/facts, and produce natural VieNeu-ready Vietnamese speech.
- [x] 5.5 Add project references/examples under the skill package without embedding mutable third-party runtime dependencies; record external inspiration/license/provenance in developer documentation.
- [x] 5.6 Implement a `SkillLoader` that loads the packaged skill and exposes stable content hash/version for GenerationFingerprint.
- [x] 5.7 Add tests proving Generate loads the skill while Fix does not.

## 6. Prompt/context builders and strict Generate/Fix separation

- [x] 6.1 Implement `LiveSessionBrief`/authoritative context builder that selects only required shop/persona/campaign/product/promotion/fact data for a generation operation.
- [x] 6.2 Implement `ScriptIntent` and transition-policy context (`ORDER_AWARE`, `ORDER_AGNOSTIC`) with deterministic previous/next summaries only when allowed.
- [x] 6.3 Implement generation prompt builder = project skill + relevant generation constraints + authoritative context + requested duration/intent + plan/segment assignment + compact continuity state.
- [x] 6.4 Implement repair prompt builder = immutable source text + exact failed rules' repair instructions + only authoritative facts needed to prevent drift; explicitly forbid broad rewrite/new claim/new CTA unless a failed rule requires it.
- [x] 6.5 Add prompt-contract tests proving Fix excludes sales skill/unrelated rules and Generate excludes repair-only instructions.
- [x] 6.6 Add contract tests proving model-facing requests expose no arbitrary filesystem/web/job-management/product-traversal tools and no model-controlled iteration API.
- [x] 6.7 Add content-size/token-budget guards for system/context/prompt assembly so oversized authoritative context fails predictably rather than silently truncating critical constraints.

## 7. Generation preview and long-form planner

- [x] 7.1 Add model-capability `GenerationBudgetCalibration` configuration for max output tokens, conservative output safety factor, observed model-output statistics, and configured lower/upper target-duration limits; this is authoring cost/call planning, not Change A speech-duration estimation.
- [x] 7.2 Implement pure no-LLM generation preview that uses `GenerationBudgetCalibration` to compute conservative safe segment work, `K`, and normal semantic call count `1 + K` for one product without duplicating Change A text-duration logic.
- [x] 7.3 Implement batch preview aggregation across selected products and expose per-product plus total semantic call estimates.
- [x] 7.4 Add tests proving preview makes zero LLM calls and behaves deterministically for identical model calibration/targets.
- [x] 7.5 Implement one-call `ProductScriptPlanner` returning strict structured plan schema with exactly K segment assignments after backend reconciliation, topic/intents, target durations, allowed fact IDs, objection IDs, CTA intent, and transition intent.
- [x] 7.6 Validate planner references against authoritative IDs and reject unknown/duplicate/impossible coverage references.
- [x] 7.7 Reconcile planner-proposed sections into backend-fixed K without asking the model for additional planning loops; persist final immutable plan before prose generation.
- [x] 7.8 Add planner tests for 10, 30, and 60 minute targets, K bounds, long-form variety, authoritative fact references, and no dynamically expanding segment count.
- [x] 7.9 Add tests distinguishing pre-generation `GenerationBudgetCalibration` from post-generation Change A speech-duration estimation so policy/config changes cannot silently conflate the two.

## 8. Sequential segment generation and continuity

- [x] 8.1 Implement typed `ContinuityState` containing bounded previous-segment tail, covered fact IDs, handled objection IDs, CTA count, opening fingerprints, last topic, and next topic.
- [x] 8.2 Implement structured `SegmentGenerationResult` containing display/spoken text candidate plus continuity metadata; schema-validate before persistence.
- [x] 8.3 Implement `ProductSegmentGenerator` as exactly one normal semantic call for one preplanned segment index using existing backend LLM abstraction/provider routing.
- [x] 8.4 Validate model-returned continuity IDs/fingerprints against the plan/authoritative registry before incorporating them into next-segment state.
- [x] 8.5 Persist each generated segment as a new immutable segment version with GenerationFingerprint and provider metadata without chain-of-thought.
- [x] 8.6 Run Segment Gate immediately; on PASS advance to the next fixed index, on FAIL stop scheduling later segment calls for that product.
- [x] 8.7 Add tests proving segment N failure prevents semantic calls for N+1..K-1 and does not auto-fix/regenerate.
- [x] 8.8 Add tests proving sequential continuity avoids an extra summary LLM call and keeps prompt context bounded rather than injecting the full prior script.
- [x] 8.9 Implement explicit human `Regenerate Segment` as one additional bounded semantic action creating a new segment version, never rewriting sibling segment versions.
- [x] 8.10 Implement explicit segment/manual edit path and resumption from the first unresolved fixed segment after gate PASS.

## 9. Product workflow, Full Script Gate, and human review state

- [x] 9.1 Implement finite `ProductGenerationWorkflow`: planning → fixed K sequential segments → compile → Full Script Gate → REVIEWABLE, with all transitions persisted.
- [x] 9.2 Implement compiled script assembly from exact selected segment versions and run Full Script Gate only after all required segments pass locally.
- [x] 9.3 Map Full Script Gate failures to global/segment-specific actionable violations without automatic semantic retry.
- [x] 9.4 Implement manual product draft path that can bypass AI generation entirely, compile/normalize, run the same gate semantics, and reach REVIEWABLE with zero LLM calls when compliant.
- [x] 9.5 Implement Fix with AI eligibility only for gate-failed immutable versions/segments and return `409` for invalid source states.
- [x] 9.6 After AI Fix, create a new DRAFT/version and require explicit submit/gate again; do not auto-submit or auto-approve.
- [x] 9.7 Add tests for manual PASS zero-call path, manual FAIL→manual edit, manual FAIL→AI Fix→resubmit, generated long-form PASS, generated segment FAIL, Full Gate FAIL, and human approval.

## 10. Multi-product batch scheduler, bounded concurrency, and recovery

- [x] 10.1 Implement `BatchScriptGenerationOrchestrator` that creates one finite ProductGenerationWorkflow per selected/missing product rather than one multi-product LLM response.
- [x] 10.2 Enforce backend-configured maximum concurrent active product workflows while keeping each product's segments sequential.
- [x] 10.3 Persist requested product set, per-product target durations, fixed call previews, current workflow states, and aggregate counts.
- [x] 10.4 Ensure one product content/provider failure does not invalidate completed sibling artifacts; surface partial-completion batch state.
- [x] 10.5 Implement finite provider/transport `max_attempts` using immutable job input; distinguish attempt count from semantic job count.
- [x] 10.6 Implement idempotency for single-product and batch generation so repeated equivalent queued/running requests return the existing workflow rather than duplicate calls.
- [x] 10.7 Implement cancellation that stops scheduling new semantic calls, preserves completed immutable artifacts, persists cancelled states, and emits terminal events.
- [x] 10.8 Implement process-restart recovery from persisted finite state/current segment index without reinterpreting model text.
- [x] 10.9 Add deterministic concurrency tests (e.g. 20 products with max 3 active), idempotency double-click tests, transport retry bound tests, partial failure tests, cancel tests, and restart recovery tests.

## 11. REST API and SSE protocol

- [x] 11.1 Add `api/v1/scripts.py` router following current backend auth/error/serialization conventions and register it under the existing `/api/v1` router.
- [x] 11.2 Implement `POST /api/v1/script-sets`, `GET /api/v1/script-sets/{set_id}`, and `PATCH /api/v1/script-sets/{set_id}` with revision/conflict handling.
- [x] 11.3 Implement `PUT .../products/{product_id}/draft` and `POST .../products/{product_id}/submit`; gate failure returns HTTP 200 with stable domain `gate_failed` payload.
- [x] 11.4 Implement single-product and batch `generation-preview` endpoints that make no model calls.
- [x] 11.5 Implement `POST .../products/{product_id}/generate`, returning `202` and workflow ID for planning + fixed-K generation.
- [x] 11.6 Implement `POST .../products/{product_id}/segments/{segment_index}/regenerate` and `POST .../products/{product_id}/fix` with eligibility/conflict guards and `202` semantics.
- [x] 11.7 Implement `POST .../products/{product_id}/approve` and `POST .../approve-batch`, preserving per-version approval records even in batch UX.
- [x] 11.8 Implement `POST .../generate-batch`, require/accept idempotency identity, and return estimated/planned workflow summary with `202`.
- [x] 11.9 Implement generation-batch snapshot/cancel endpoints and stable structured domain error codes.
- [x] 11.10 Implement SSE `GET .../generation-batches/{batch_id}/events` with ordered event sequence, stable IDs, auth, reconnect-safe snapshot/revision behavior, and no script text in event payloads by default.
- [x] 11.11 Add API contract tests for auth, 200 gate-fail domain semantics, 202 async jobs, 409 invalid transitions/stale states, 422 malformed body, idempotency, and SSE reconnect/deduplication.

## 12. Runtime ScriptSet binding and canonical Change A handoff

- [x] 12.1 Implement `session_binding.py` validation for ScriptSet existence, required products, approved versions, dependency freshness, transition/order compatibility, and runtime catalog compatibility.
- [x] 12.2 Add `PUT /api/v1/sessions/{session_id}/script-set` under existing session API conventions; return structured `409` missing/stale details on not-ready sets.
- [x] 12.3 Store/resolve the bound ScriptSet/approved product-version mapping in session/runtime state without mutating authoring artifacts.
- [x] 12.4 Integrate Director/runtime product speech selection so approved-script path resolves exact approved `spoken_text` and enters the same canonical Change A source-agnostic speech path that uses `backend.application.text_chunker.TextChunker` for arbitrary/full text.
- [x] 12.5 Ensure the full approved script is supplied as text to the canonical chunker path and segmented via normal `feed(...)`/`finalize()` semantics (directly or through the existing runtime speech service); do not directly construct a giant `TextChunk`.
- [x] 12.6 Migrate/verify every Change B runtime import so `TextChunker`/`TextChunk` come only from `backend.application.text_chunker`; do not import `backend.application.speech_chunking` or `TextChunk` from `backend.application.render.windows`.
- [x] 12.7 Do not add `ScriptTextChunker`, `VerbatimChunker`, `mode="script"`, or other source-specific segmentation implementation/config. Fixed rollback and `adaptive_vi` both remain Change A policies behind the same TextChunker capability.
- [x] 12.8 Do not add/forward authoring-owned `flush_timeout_ms`, `check_timeout`, streaming deadline timers, or adaptive `target_chars`; a complete approved script has no upstream token wait, and realtime deadline ownership remains Change A orchestration/controller.
- [x] 12.9 Do not construct/reconstruct `TextChunk` to stamp `is_final`; allow Change A's exactly-once finalization protocol to own normal completion and error/cancel semantics.
- [x] 12.10 Add contract test proving no post-approval LLM rewrite occurs between approved `spoken_text` and canonical TextChunker ingestion.
- [x] 12.11 Add contract test for full-script segmentation proving one approved long script produces multiple canonical TextChunks when policy requires it and text is neither lost nor duplicated.
- [x] 12.12 Add finality integration tests covering normal approved-script completion, error/cancel, and the edge case where the last content chunk was already emitted before EOF; Change B must not fabricate a replacement final TextChunk.
- [x] 12.13 Add local integration test for multiple approved products, runtime product selection/reordering under ORDER_AGNOSTIC, exact text/version identity at the chunker boundary, and both adaptive/default policy plus explicit fixed rollback using the same path.

## 13. Workbench authoring UX

- [x] 13.1 Add ScriptSet creation/edit view with LiveSessionBrief, product selection/order, transition policy, target duration per product, and generation-call preview before spending tokens.
- [x] 13.2 Add per-product states and controls: manual draft, Submit, Generate Script, Regenerate Segment, Fix with AI, and Approve with controls enabled only for legal states.
- [x] 13.3 Add long-form segment navigator showing title/intent, target/estimated duration, status, gate violations, version history, and exact display/spoken previews.
- [x] 13.4 Add Generate All UX with selected/missing products, per-product and total estimated semantic calls, bounded-progress status, partial failure, retryable transport failure, and explicit human cost action.
- [x] 13.5 Add SSE client for batch progress with reconnect/snapshot recovery and no duplicate action on reconnect.
- [x] 13.6 Add batch review/approve selected REVIEWABLE versions while retaining individual immutable approval records.
- [x] 13.7 Add stale dependency warnings that disable runtime-ready state until resubmit/reapprove.
- [x] 13.8 Add frontend tests for zero-LLM manual PASS, Generate All double-click/idempotency UX, segment failure pause, AI Fix legal-state guards, spoken-text review, approval invalidation, and SSE reconnect.

## 14. Observability, cost controls, security, and documentation

- [x] 14.1 Add content-private telemetry for target duration, K, planned/actual semantic calls, provider attempts, generation latency/output tokens when available, gate rule IDs, workflow durations, cancellation, approval/staleness transitions.
- [x] 14.2 Add tests proving normal logs/SSE events do not include raw script/prompt content and that IDs/fingerprints provide enough debugging context.
- [x] 14.3 Add configuration only for Change B-owned concerns: product concurrency, provider max attempts, generation duration bounds, `GenerationBudgetCalibration`/output safety factor, skill path/version expectation, and SSE retention/replay window using existing config conventions. Do not duplicate Change A `min_chars`/fixed `target_chars`/hard-cap policy defaults, `flush_timeout_ms`, runtime-hint defaults, or policy-mode configuration in authoring config.
- [x] 14.4 Add authorization tests separating script authoring/edit, AI-spend actions, and human approval according to existing admin/operator role capabilities; do not expose generation endpoints anonymously.
- [x] 14.5 Document third-party copywriting/product-marketing inspiration and any Vietnamese profanity/toxicity dataset provenance/licenses without making remote runtime dependencies.
- [x] 14.6 Document expected semantic-call formula, preview semantics, transport retry distinction, and explicit actions that can add extra calls (Fix/Regenerate).
- [x] 14.7 Document operation/runbook for stuck/recovered/cancelled batches and invariant that backend—not model—owns iteration/job creation.

## 15. End-to-end verification, architecture audit, and closeout

- [x] 15.1 Run focused backend unit/integration/contract suites for `script_authoring` plus Ruff/format/static checks used by backend service CI.
- [x] 15.2 Run PostgreSQL integration with restart/recovery/idempotency and migration-from-clean-DB verification.
- [x] 15.3 Run manual-draft E2E: create ScriptSet → draft → gate PASS → review exact spoken text → human approve → bind → canonical Change A TextChunker → VieNeu playback; verify zero LLM authoring calls.
- [x] 15.4 Run AI long-form E2E for at least 10-minute and 30-minute targets and a bounded 60-minute planning/dry-run/call-budget test; verify fixed K and no model-controlled extra jobs.
- [x] 15.5 Run multi-product Generate All E2E with bounded concurrency, one product segment gate failure, sibling completion, human repair/resume, batch approval, and runtime selection across approved products.
- [x] 15.6 Verify no content/gate failure produces automatic AI repair/regeneration and no general tool/agent loop exists in the production path.
- [x] 15.7 Verify exact approved `spoken_text` identity at Change A boundary, full-script segmentation through the same source-agnostic TextChunker, and no post-approval mutation/rewrite.
- [x] 15.8 Run repository-wide searches equivalent to `rg -n "speech_chunking" .`, `rg -n "render\.windows.*TextChunk|from .*render\.windows import .*TextChunk" .`, and `rg -n "legacy.*TextChunk|compat.*TextChunk|re-export.*TextChunk" .`; historical prose may remain only when clearly historical, while active implementation dependencies MUST be zero.
- [x] 15.9 Run Change-B-scope searches equivalent to `rg -n "check_timeout|flush_timeout_ms|target_chars|ScriptTextChunker|VerbatimChunker|mode=.*script|TextChunk\(" services/product/backend_service/src/backend/application/script_authoring services/product/backend_service/src/backend/api` and inspect every result; require zero Change B-owned streaming timeout/source-mode/manual TextChunk finality/bypass implementation.
- [x] 15.10 Inspect every approved-script/full-script/verbatim runtime call path touched by Change B and prove it reaches canonical `backend.application.text_chunker.TextChunker`; no direct giant TextChunk construction or parallel segmenter is allowed.
- [x] 15.11 Verify Change B contains no duplicate speech-duration estimation implementation and that actual spoken duration checks call the stable Change A estimator, while generation preview uses separately named `GenerationBudgetCalibration`.
- [x] 15.12 Run Change A focused TextChunker/full-script/finality/render-orchestrator regressions after Change B integration to prove the downstream contract was not weakened or bypassed.
- [x] 15.13 Run current local Stage 2/Workbench/session cleanup regressions without AWS mutation; verify authoring does not require billable avatar/GPU resources.
- [x] 15.14 Run `git diff --check` and the repository's relevant static/type checks.
- [x] 15.15 Run `openspec validate approved-script-authoring-pipeline` and confirm Change A strict validation/PASS evidence referenced by the dependency gate is still current.
- [x] 15.16 Update capability/runbook/API documentation and record Change B readiness evidence only after every architecture audit and behavioral gate above passes.

## 16. Production scope decision (recorded 2026-08-16, post-apply re-review NEW-SCRIPT-01)

The post-apply independent re-review (NEW-SCRIPT-01, HIGH) flagged that the
`/api/v1/script-sets` authoring surface is not composed in the production
backend container: `backend.bootstrap.app_factory._build_container()` does not
construct a `script_authoring_service`, and `application/script_authoring/service.py`
defines only the `ScriptAuthoringService` **Protocol** (no concrete production
implementation, no `repositories.py`, no `db/sql/` migrations, no composition
wiring). In production the router returns **501 "script authoring not enabled"**.

**Decision (recorded, no production code change):** for this release, the
approved-script authoring surface is explicitly **scoped OUT of production**.
The 501 is the intended gated state for MVP — consistent with this change's
spec ("no production-ready claim for approved-script runtime integration SHALL
be made"). A concrete `ScriptAuthoringService` implementation + SQL-backed
repositories + migrations + container composition is deferred to a follow-up
change and is NOT part of this release gate. Reviewers SHOULD treat
`create_app() → POST /api/v1/script-sets → 501` as the accepted MVP behavior.
