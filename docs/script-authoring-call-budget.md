# Script Authoring — Semantic Call Budget

Expected model (semantic) call consumption for the authoring pipeline, the
no-LLM preview semantics, and the transport-retry distinction. This is the
call/cost contract the Workbench surfaces to the human **before** any
tokens are spent (design Decision 11).

## 1. Semantic-call formula: `1 + K` per product

One product workflow consumes exactly **1 planning call + K generation
calls**:

- **1 planning call** — `ProductScriptPlanner` (task 7.5) produces the
  strict structured plan: exactly K segment assignments, topic/intents,
  target durations, allowed fact/objection IDs, CTA intent, transition
  intent. Backend reconciliation fixes K (task 7.7); the model never asks
  for additional planning loops.
- **K generation calls** — one normal semantic call per fixed segment
  index (`ProductSegmentGenerator`, task 8.3), sequentially, with compact
  `ContinuityState` between segments. No summary call between segments.

So the expected semantic call count for a single product is:

```text
planned_semantic_calls = 1 + K
```

For a batch of N products, the total is the sum of per-product `1 + K`
(aggregated by `BatchScriptGenerationOrchestrator`, task 10.1).

## 2. Preview semantics — no LLM

The `generation-preview` endpoints (task 11.4) make **zero model calls**.
They compute the plan-level estimate from:

- selected product target durations;
- `GenerationBudgetCalibration` (task 7.1): provider max output tokens,
  conservative output safety factor, observed model-output statistics,
  configured lower/upper target-duration limits;

using **separately named** calibration — NOT Change A's deterministic
speech-duration estimator, which is used only **after** text exists to
check actual spoken duration (task 7.9). Preview output is per-product
plus total estimated semantic calls and is deterministic for identical
calibration/targets (task 7.4).

## 3. Transport retry — NOT a semantic retry

Transport/provider failures (timeouts, 5xx, connection errors) MAY be
retried up to the configured finite `provider_max_attempts` (task 10.5),
**with the same immutable generation input** and the same idempotency/job
identity where provider semantics permit. Retries do not change the
semantic call count — the call budget counts **jobs**, not transport
attempts:

```text
provider_attempts (transport)  !=  semantic_calls (jobs)
```

Content/gate failures are **never** retried automatically: the workflow
becomes `GATE_FAILED` / `FAILED_CONTENT` and awaits a human command
(design Decision 12).

## 4. Explicit actions that add bounded extra calls

The base formula covers the normal workflow only. Two explicit **human
actions** may add exactly one bounded semantic call each:

| Action | Extra calls | Bound | Semantics |
| --- | --- | --- | --- |
| `Fix` (task 9.5) | +1 | one repair call on a gate-failed immutable version | immutable source text + failed rules' repair instructions + authoritative facts only; never a broad rewrite |
| `Regenerate Segment` (task 8.9) | +1 per segment | one call creating a new immutable segment version | never rewrites sibling segment versions |

These are explicit user commands surfaced in the Workbench before
spending, and the preview estimate explicitly excludes them (design
Decision 11: "excludes explicit future user actions").

### Summary

```text
base workflow        1 + K                    semantic calls per product
batch                sum(1 + K_i)             across selected products
transport retries    attempts <= max_attempts same immutable input, not semantic
Fix                  +1                       bounded, explicit human action
Regenerate Segment   +1 per segment           bounded, explicit human action
preview              zero model calls         GenerationBudgetCalibration only
```
