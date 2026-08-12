# Script Authoring — Operations Runbook

Recovery, cancellation, and ownership invariants for the authoring
pipeline in production. The governing principle (design Decision 20):

> Generation state SHALL survive API worker restart. The backend resumes
> only from a persisted finite next step; it does not reconstruct control
> flow from model prose. The backend — never the model — owns iteration and
> job creation.

## 1. Recovery from persisted finite state

Every batch/workflow persists a finite, machine-readable state (task
10.3/10.8): immutable input fingerprint, requested product set, per-product
target durations, fixed call previews, planned segment count, **current
segment index**, attempt counts, status, and generated version references.

On API-worker restart:

1. **Reconstruct from state, not prose.** The backend reads the persisted
   current segment index and resumes at the first unresolved fixed segment
   (task 8.10). Model text is never reinterpreted to derive control flow.
2. **No double-spend.** A repeated equivalent queued/running request
   (idempotency key, task 10.6) returns the existing workflow instead of
   starting new semantic calls.
3. **Completed artifacts are kept.** Persisted immutable segment versions
   and gate results remain valid; recovery never rewrites them.

**Runbook action — stuck batch:** if a batch appears stuck, check the
persisted workflow state (status, current segment index, last heartbeat /
event). A worker restart resumes automatically from that state; do NOT
resubmit the generation request blindly — resubmission with the same
idempotency identity returns the existing workflow, and a *new* identity
starts a fresh workflow with new calls.

## 2. Cancellation semantics

Cancellation (task 10.7) is a terminal, persisted operation:

- stops **scheduling new** semantic calls (in-flight calls complete or
  are abandoned per provider semantics);
- preserves completed immutable artifacts;
- persists the cancelled state for the workflow/batch;
- emits a terminal SSE event (task 11.10).

**Runbook action — cancel:** use the batch snapshot/cancel endpoint (task
11.9). After cancel, the batch is terminal; resume by starting a new
workflow with explicit human confirmation (new idempotency identity),
reusing completed sibling artifacts where applicable (task 10.4).

## 3. Content failure awaits a human command

Content/gate failures are **never** auto-repaired or auto-regenerated
(design Decision 12, task 15.6):

- segment gate FAIL stops scheduling segments N+1..K-1 for that product
  (task 8.6/8.7);
- full-script gate FAIL maps to actionable violations **without automatic
  semantic retry** (task 9.3);
- the workflow enters `GATE_FAILED` / `FAILED_CONTENT` and **awaits a
  human command**: manual edit, `Fix`, or `Regenerate Segment` (each
  bounded and explicit, see the call-budget doc).

**Runbook action — content failure:** do not resubmit blindly. Review the
gate violations (stable rule IDs + rule-set fingerprint, surfaced via
telemetry and the Workbench), apply a human command, then resubmit.

## 4. Backend owns iteration and job creation — no agentic loop

- **The model has no control plane.** Model-facing requests expose no
  arbitrary filesystem/web/job-management/product-traversal tools and no
  model-controlled iteration API (task 6.6).
- **K is fixed by the backend.** The planner's K is reconciled
  backend-side (task 7.7); the model cannot expand the segment count or
  spawn jobs.
- **One finite workflow per product.** `BatchScriptGenerationOrchestrator`
  creates one finite `ProductGenerationWorkflow` per product (task 10.1)
  under the configured `max_concurrent_products` bound (task 10.2).
- **Zero automatic AI repair in the production path** — any content/gate
  failure pauses and surfaces to the human (task 15.6).

**Runbook action — suspected agentic loop:** verify no post-approval LLM
rewrite occurs between approved `spoken_text` and canonical Change A
`TextChunker` ingestion (task 15.7), and confirm cancellation stops new
semantic scheduling. The backend is the only job creator; there is no
model-controlled loop to unwind.
