## Why

A commerce livestream commonly sells multiple products, and a human host may spend roughly 10–60 minutes on a single product. The current project does not have a durable pre-live authoring workflow for creating, validating, reviewing, approving, versioning, and binding long-form sales scripts to a live session. Relying on realtime LLM generation keeps model latency, output limits, hallucination risk, and provider failures on the critical path to speech.

The desired product direction is **gate-first, AI-optional, human-final**. A human-written draft that already satisfies deterministic policy should pass directly to review without paying for an LLM call. AI is invoked only when the user explicitly asks to generate missing content or fix a failed draft. Long-form generation must not depend on a general agentic loop: the backend must know and bound the number of semantic model calls before generation starts, retain control of retries and traversal, and prevent hidden tool-calling or unbounded loops.

This authoring pipeline also needs to work across many products in one planned live. The UX should support one-click batch generation while inference remains isolated per product and per preplanned segment, so each product retains sufficient detail and output budget. Every generated or manually edited artifact must pass deterministic ScriptGate checks and a human approval step before its `spoken_text` may reach Change A `adaptive-speech-text-chunking`, the provider-neutral Change T runtime contract, and the avatar.

## What Changes

- Add a pre-live **`ScriptSet` authoring aggregate** representing the scripts, ordering/configuration, and review state for one planned livestream. Authoring is independent of runtime `session_id`; an approved ScriptSet is bound to a live session only when runtime starts.
- Add deterministic **ScriptGate** validation with a versioned shared `ScriptRuleRegistry`. The same canonical rules provide:
  - deterministic gate checks;
  - generation constraints for AI-generated scripts;
  - minimal repair instructions for only the rules a draft failed.
- Make the normal workflow **gate-first and AI-optional**:
  - user writes/edits `ScriptDraft` → submit → ScriptGate;
  - PASS → `REVIEWABLE` without any LLM call;
  - FAIL → show violations; user may fix manually or explicitly press **Fix with AI**;
  - no draft → user may explicitly press **Generate Script** or **Generate All**.
- Add two deliberately different LLM contracts:
  - **Generate** = LLM + project-owned `livestream-sales-script` skill + generation rules + authoritative product/shop/campaign context;
  - **Fix** = LLM + only relevant failed rules + authoritative facts, with a minimal-repair contract and no sales-copy skill.
- Add a project-owned runtime skill at `services/product/backend_service/resources/skills/livestream-sales-script/SKILL.md`, adapted from reviewed copywriting/product-marketing principles rather than fetched from a remote skill at runtime.
- Support **10–60 minute product scripts** through bounded hierarchical generation:
  - one short `ProductScriptPlan` LLM call;
  - backend computes a fixed segment count `K` from requested spoken duration and a calibrated safe output budget;
  - exactly `K` preplanned segment generation jobs run for that product, sequentially for continuity;
  - no model-controlled `while continue`, tool loop, or dynamically expanding segment count.
- Add deterministic **generation preview** so the UI can show target duration, planned segment count, and estimated semantic LLM calls before the user spends tokens.
- Add compact `ContinuityState` between adjacent segments so long scripts keep a coherent sales arc without injecting the full prior script into every prompt or requiring an additional summary-model call.
- Run **Segment Gate** after each generated segment and stop that product workflow on failure before spending calls on later segments. After all segments pass, compile the product script and run a **Full Script Gate** for cross-segment repetition, coverage, contradictions, CTA pacing, tone, and total-duration checks.
- Add deterministic multi-product **batch orchestration**:
  - one-click `Generate All` fan-out across selected/missing products;
  - bounded product-level concurrency;
  - sequential segments within each product;
  - per-product isolation and progress;
  - no LLM-controlled job creation, traversal, retry count, or tool calls.
- Add immutable script versions, segment versions, generation fingerprints, gate runs, approvals, batch/job state, and dependency versions. Any edit creates a new draft/version instead of mutating approved content.
- Distinguish `display_text` from TTS-ready `spoken_text`; human reviewers MUST see the exact normalized spoken representation that will be approved and spoken.
- Make **human approval mandatory** after gate PASS. Gate PASS means `REVIEWABLE`, never automatically `APPROVED`.
- Bind approvals to exact content and dependencies. A change in spoken text, product facts, promotion facts, persona/context, rule-set version, or other configured approval dependency invalidates/stales the approval.
- Add REST/JSON authoring APIs under `/api/v1/script-sets`; return `202 Accepted` for asynchronous AI jobs; add SSE for one-way batch/job progress. Do not add MCP/A2A/general agent protocol or reuse the runtime avatar WebSocket for authoring progress.
- Add an explicit session binding endpoint so a runtime session consumes only fresh approved script artifacts and passes approved `spoken_text` into Change A in-process.
- Integrate only through Change A's final canonical package **`backend.application.text_chunker`**. A complete approved script is an arbitrary/full text input to the same source-agnostic `TextChunker`; Change B MUST NOT construct one giant `TextChunk`, create a script-specific chunker, deep-import a parallel `speech_chunking` namespace, use `render.windows.TextChunk`, or bypass Change A segmentation.
- Keep Change A timing/policy/finality ownership intact: authoring does not own streaming deadlines, `flush_timeout_ms`, adaptive `target_chars`, source-specific chunker modes, or manual `is_final` stamping. Change B reuses Change A's canonical deterministic speech-duration estimator for generated-text duration checks instead of implementing a second estimator.
- Add Vietnamese format/style/spelling/TTS-readiness/profanity/toxicity/commerce-claim rule families. Any external lexicon/dataset-derived resource must have provenance/license review and be curated/versioned before runtime use; the runtime must not treat a raw toxic dataset as a direct blacklist.
- Add cost, retry, idempotency, cancellation, recovery, and observability semantics so batch spend and job behavior remain bounded and diagnosable.

## Capabilities

### New Capabilities

- `approved-script-authoring-pipeline`: Pre-live multi-product script drafting, deterministic validation, optional AI generation/repair, bounded long-form generation, human approval/versioning, REST+SSE workflow APIs, and runtime binding to approved `spoken_text`.

### Modified Capabilities

- *(none — the current repository does not expose an established `openspec/specs/` capability for pre-live script authoring)*

## Dependency and Sequencing

This is **Change B** and is hard-blocked by **Change A `adaptive-speech-text-chunking`**.

Implementation of Change B MUST NOT begin until Change A has completed **both** its architecture-correction gate and its VieNeu benchmark PASS gate. Required upstream evidence includes:

- final cohesive package `backend.application.text_chunker/` with `__init__.py`, `chunker.py`, `types.py`, `boundaries.py`, `duration.py`, and `policy.py`;
- no active parallel `backend.application.speech_chunking` implementation namespace and no sibling `backend/application/text_chunker.py` facade;
- exactly one canonical `TextChunk`, exported by `backend.application.text_chunker`; `render.windows` neither defines nor re-exports it;
- complete/full-script/verbatim speech paths already use the same source-agnostic `TextChunker` rather than directly constructing a giant `TextChunk`;
- realtime deadline ownership lives in streaming orchestration/controller, not in `TextChunker`;
- fixed and `adaptive_vi` are clean policy strategies with typed policy-specific configuration; fixed `target_chars` is not leaked into adaptive policy;
- configuration defaults are centralized under Change A's canonical typed configuration;
- normal-completion finality is explicit/exactly-once without manual legacy `TextChunk` reconstruction;
- Change A focused/regression/static checks and strict OpenSpec validation are green;
- Change A has recorded VieNeu benchmark PASS under its acceptance rule.

If any architecture-correction item remains incomplete, or Change A is NOT PASS, Change B remains blocked even if a temporary compatibility path happens to work. Change B MUST NOT compensate by adding its own shim, duplicate type, alternate chunker path, or deep import.

Change A owns source-agnostic speech segmentation, its canonical `TextChunk` protocol, fixed/adaptive policy strategy, deterministic speech-duration estimation, streaming-controller deadline semantics, runtime TTS/playback hints, and exactly-once TextChunk→AudioWindow→VideoWindow finality. Change B owns authoring, validation, LLM generation/repair, long-form generation planning, versioning, human approval, and selection of the exact approved `spoken_text` handed into the canonical Change A speech path.

## Impact

- **Backend application**: add `script_authoring` application subsystem under `services/product/backend_service/src/backend/application/`.
- **Runtime resources**: add project-owned `livestream-sales-script/SKILL.md` under `services/product/backend_service/resources/skills/`.
- **API**: add `/api/v1/script-sets` REST resources and SSE batch events; add a session script-set binding command under existing `/api/v1/sessions/{session_id}` conventions.
- **Persistence**: add SQL schema/repositories for ScriptSet, product script items/plans/segments/compiled versions, gate runs, approvals, generation batches/jobs, and dependency/fingerprint metadata.
- **LLM integration**: reuse the backend's existing LLM abstraction/provider routing; do not let model output invoke tools or control backend iteration.
- **Workbench**: add pre-live script-set creation/editing, gate results, Generate/Fix/Generate All, duration/call preview, segment-level progress/review, spoken-text preview, and human approval controls.
- **Safety**: deterministic gate remains authoritative; AI output is always a draft and must be re-gated; raw external moderation datasets are not trusted as runtime policy without curation/provenance review.
- **Cost controls**: semantic call budgets are computed/displayed before long-form generation; content failures do not trigger automatic AI repair/regeneration loops; provider retries are separately bounded.
- **Change A integration**: runtime imports only the canonical `backend.application.text_chunker` package contract (or calls the existing runtime speech service that already does so); there is no `speech_chunking` compatibility namespace, `render.windows.TextChunk` path, script-specific chunker, or direct `TextChunk(...)` construction in Change B.
- **Duration ownership**: generated-text duration validation reuses Change A's canonical deterministic speech-duration estimation interface. Pre-generation model-output budgeting remains a separate authoring calibration and MUST NOT become a duplicate speech estimator.
- **Runtime**: live session reads only approved/fresh `spoken_text` and feeds the complete text through the same Change A source-agnostic `TextChunker` path used for arbitrary input; script authoring does not require avatar/TTS/live session resources and does not own streaming timeout/finality mechanics.
- **Out of scope**: general agent framework, arbitrary LLM tools, autonomous web research, automated human approval, realtime viewer Q&A authoring, model fine-tuning, neural toxicity/spell models as a mandatory first release dependency, and AWS deployment changes.
