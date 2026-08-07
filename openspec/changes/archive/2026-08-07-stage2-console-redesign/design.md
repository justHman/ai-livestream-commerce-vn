## 1. Operator Flow and Runtime Revisions

### Page Bootstrap (no network)
- Viewer/Admin tokens prefill from fixture JS, not persisted to localStorage.
- Four products + shop presets render immediately from local fixture JS.
- No mock API calls, no "loaded" log entries.
- localStorage hydrates versioned draft: shop profile, product order, test preferences. Incompatible schema → fixture defaults.

### Start + Attach (manual only)
- Auto Demo never auto-starts or auto-attaches.
- First Attach sends selected+ordered catalog + structured shop profile.
- Backend validates and stores atomic snapshot with `profile_revision` and `catalog_revision`. Returns `will_speak=false`.
- Log/diagnostics show full snapshot and prompt layers.
- Live Re-attach: profile + catalog updated atomically; speaking turn completes; removed product → advance per new order; new product → append; new profile → next turn; opening/checkpoint preserved; stale prepared turns invalidated by revision token.

### Runtime Configs
- All controls (rate, ingest mode, Q&A window limits, cooldown, cache variants, prepared depth, retry count, pivot thresholds) editable while session is live.
- Speaking turn finishes; next turn uses new revision.
- Config logic lives in backend session/store, not localStorage.

## 2. FSM — Opening, Product Stages, Q&A, and Pivot

### Global Opening (protected gate)
Three LLM-grounded turns, cached per profile + catalog revision:
1. Shop, MC, profile introduction.
2. Engagement prompt (like/share/comment/follow/view).
3. Ordered catalog overview and agenda.
Comments ingested/clustered during opening but NOT played until all three turns' `speak_ended` confirmed.

### Per-Product Lifecycle
```
Intro → Benefit (1..N) → [Q&A window] → Offer → [Q&A window]
  → Trust → [Q&A window] → CTA → [Q&A window] → Transition
```
- Intro + Benefit 1 is a protected gate before any Q&A.
- Q&A window: size>=2 clusters ranked by scorer (product relevance, intent actionability, size, recency, phase, new demand). Each window: max 2 clusters or 45s hard timeout. Queue exhausted → close early.
- Singleton retained in rolling window for later merge.

### Q&A Phrasing
- Paraphrase shared intent into one clause, not verbatim member replay.
- Answer 1–2 grounded sentences.
- Topic cooldown default 120s; new comments with distinct content reset eligibility.
- Answer variants: configurable default 3 per (product, topic, fact/profile revision). Round-robin session cache after variants exhausted.

### Cross-Product Handling
- **Q&A excursion**: product B cluster wins ranking but demand not overwhelming → answer one Q&A B, return to checkpoint A.
- **Demand pivot**: B demand >=60% share, min 5 unique comments, margin >=0.15 → checkpoint A, full B lifecycle (no global opening), resume A when B exits <45%.
- If B still hot after full lifecycle → new B lifecycle with fresh variants.
- Never nested pivot; demand C queued.
- Hysteresis 60/45 prevents oscillation.

## 3. Continuous Loop, Prepared Queues, and Errors

### Auto Demo Producer
- Requires session Start + Attach manually.
- First launch: fetches 1010 mock comments. Initial mode: batch 20 or individual from start.
- After seed, continuous producer: 0.2–5 comments/s, default 0.67. `setTimeout` after prior request completes; rate changes effective next tick.
- Pool exhausted → repeat with new IDs/timestamps.
- Feed: newest 20 rows only. Backend rolling window is time-based.
- Stop: immediate interrupt, producer stopped, pending requests/inflight preparation invalidated. Session stays attached.

### Three-Tier Speech Pipeline
```
Decision Queue (ranked truth)
  → Preparation Queue (LLM/TTS)
    → Serialized Playback Queue
```
- Prepared depth configurable, default 3.
- Coordinator continues ingest/cluster/rank during avatar playback.
- Cloud: prepare script + PCM; enqueue next utterance; dequeue after `speak_ended`.
- Self-host: render media windows ahead; consumer plays sequentially.
- Revision tokens increment on config/profile/catalog/pivot change.
- Adapter cancel best-effort; uncancellable work → drop stale results on completion.
- Diagnostics show queued/preparing/prepared/playback/completed/failed/stale.

### Error Policy
- **Transient** (LLM/TTS/network/playback): retry max 1, typed lifecycle. Not marked answered/advanced before success.
- **Terminal** (session/auth/closed): stop producer, clear queues, cleanup, UI → failed.
- **Validation** (config/data): fail-fast with typed field error; preserve old draft/runtime.
- **Stale cancellation**: `cancelled_stale` state, not provider failure.
- **Playback timeout**: separated from other timeouts.
- Session stop success requires all tasks/queues/cache/locks/store/LiveAvatar/LiveKit/local UI clean.

## 4. Diagnostics, Benchmark, and Verification

### Selected Cluster Display
```
"1 selected cluster"
- Cluster ID / topic / product / intent
- Size + all members
- Total score + per-factor breakdown
- Prompt layers (4)
- Paraphrased question
- Generated/cached script + cache variant/index
- Revisions (profile, catalog, config, generation)
- Queue lifecycle + timestamps
```

### Reusable Benchmark Harness
Same fixture, config, and FSM as Auto Demo — no separate fake flow.

Three lanes:
1. **Offline deterministic**: hash/fake embedder + fixed LLM/TTS + mock renderer. Exact state/queue/pivot/revision/error verification.
2. **Local real full loop**: semantic VN embedder + real LLM/TTS + mock renderer. Coverage target: 3 opening turns, one full product lifecycle, >=2 Q&A windows, excursion, demand pivot + resume. Hard timeout 10min.
3. **Sandbox smoke**: credentials + LiveKit + 1–3 playback-confirmed turns only. Not full-lifecycle evidence.

Metrics per run/session/turn/revision: ingest latency, tick/queue wait, embed/cache, route/cluster/rank, decision wait, LLM TTFT/total, TTS first audio/total/RTF, preparation depth/stale/cancel, avatar start→end, end-to-end, drops/underflow/retries/errors, cleanup.

Report to gitignored `.runtime/benchmarks/stage2/`. FAIL when p95 stage regression >20% vs same-profile baseline. Report identifies critical-path bottleneck.

### Final Verification
- TDD focused tests then full `core/tests/`.
- Ruff changed Python.
- Node parse inline JS.
- OpenSpec validate.
- E2E: start backend (shared dl env) + start static FE. No automatic browser opening. Send URL only.
