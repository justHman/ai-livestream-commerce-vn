# Design: Multi-Session Batched TTS Runtime

## Context

`tts_service` must evolve from a single-provider vLLM/vLLM-Omni wrapper into a reusable TTS serving service that can keep one GPU productive across many concurrent livestream sessions. Prepared scripts make this especially valuable: Change A can make many speech chunks available ahead of playback, while the backend may keep a bounded window of per-chunk HTTP requests in flight. If batching remains in each backend/session, GPU capacity is fragmented across small session-local batches. The service that owns the GPU must own admission and batching globally.

VieNeu v3 Turbo is the first provider. Current upstream behavior matters to the design:

- `Vieneu()` defaults to v3 Turbo and auto-selects ONNX on CPU versus PyTorch on CUDA-capable hosts.
- public `infer_batch(texts, voice=..., style=...)` shares one resolved voice/style across all texts and is sequential on ONNX/CPU;
- the GPU/PyTorch path exposes a static `V3TurboBatchEngine.generate_batch(requests, ...)` whose individual requests carry their own `speaker_emb`, `ref_codes`, and `style`;
- one `generate_batch()` call is static: requests that arrive after dispatch cannot join the in-flight batch.

Therefore Change T implements **continuous admission + successive dynamic micro-batches** around the provider's static batch engine. It does not claim true vLLM-style token/frame-level continuous insertion into a running VieNeu batch.

The user-provided Tesla T4 measurements are a useful engineering anchor: direct VieNeu `infer_batch` scaled from about 1.45x realtime at batch=1 to about 12.58x realtime at batch=32. The service-level benchmark must quantify how much of that direct-provider throughput survives scheduler/API overhead.

## Goals

1. Make the backend-facing TTS API provider-neutral and stable.
2. Move all GPU batch scheduling into `tts_service`.
3. Batch compatible requests across independent sessions, including different preset/cloned voices when the provider can support per-request conditioning.
4. Keep request count, routing, queueing, fairness, and overload behavior deterministic and observable.
5. Support tenant-scoped voice cloning without forcing cloned-voice sessions onto permanently unbatchable singleton lanes.
6. Preserve CPU/ONNX as a functional fallback without pretending it has GPU batch scaling.
7. Establish multi-session correctness and performance evidence before Change B relies on the runtime.

## Non-goals

- `/ws/platform`, viewer-message selection, Director Q&A, or interruption workflow.
- Frame-streaming TTS as the primary production execution mode.
- True in-flight request insertion into VieNeu's static batch engine.
- TextChunker changes; Change A is complete.
- Backend-created `/batch` requests.
- An arbitrary provider failover mesh.
- A new general workflow/agent system.

## Final service architecture

```text
services/product/tts_service/
├── contracts/v1/
│   └── ... stable request/response contract fixtures ...
├── src/tts/
│   ├── api/
│   │   ├── routes.py
│   │   ├── schemas.py
│   │   └── voices.py
│   ├── bootstrap/
│   │   └── ... dependency wiring ...
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── capabilities.py
│   │   └── vieneu_v3.py
│   ├── scheduler/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── admission.py
│   │   ├── fairness.py
│   │   └── runtime.py
│   ├── voices/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── store.py
│   │   └── enrollment.py
│   ├── observability/
│   │   └── ... metrics/logging ...
│   ├── config.py
│   └── main.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── load/
└── scripts/
    ├── benchmark_provider.py
    └── benchmark_multisession.py
```

Existing vLLM/vLLM-Omni-specific runtime code may be deleted or migrated during the change. The final active execution path MUST flow through `TTSProvider`; backend callers must not depend on provider implementation modules.

## Core request model

Internally, every synthesis call becomes a `SynthesisRequest` conceptually containing:

```text
request_id
session_id
utterance_id
chunk_seq
input_text
voice_profile_id
style
priority
response_format
generation_config
deadline_at
submitted_at
```

The HTTP request remains one speech chunk. `session_id`, `utterance_id`, and `chunk_seq` are scheduling/tracing metadata; they are not provider controls. The response to each HTTP request contains only the audio/result for that request.

No client constructs a GPU batch. Multiple simultaneously waiting HTTP requests are the scheduler's input population.

## Stable external API

### Synthesis

```http
POST /v1/audio/speech
Content-Type: application/json
```

Conceptual request:

```json
{
  "input": "Mọi người nhìn sản phẩm này nhé. [cười]",
  "voice_profile_id": "vp_01...",
  "style": "natural",
  "priority": "normal",
  "session_id": "sess_01...",
  "utterance_id": "utt_01...",
  "chunk_seq": 4,
  "response_format": "wav"
}
```

The service waits until the request is selected, synthesized, and encoded, then returns audio on the same HTTP request. Batching is invisible to the caller.

### Health/readiness/capabilities

```http
GET /health
GET /ready
GET /v1/audio/capabilities
```

`/health` proves the process is alive. `/ready` proves the selected provider/model/profile subsystem and scheduler are ready to accept synthesis. Capability discovery exposes provider-neutral facts such as available styles/cue names, cloning support, sample rate, and whether the current accelerator provides native batch throughput.

### Voice profiles

```http
POST   /v1/voices
GET    /v1/voices/{voice_profile_id}
DELETE /v1/voices/{voice_profile_id}
```

Enrollment accepts a bounded reference WAV plus provider-neutral metadata, returns an opaque tenant-scoped `voice_profile_id`, and persists the provider-specific enrolled representation. A backend must never receive VieNeu `speaker_emb` or `ref_codes`.

Preset voices are represented through the same `voice_profile_id` abstraction, so backend call shape does not change between a preset voice and a cloned voice.

There is no required public `/v1/audio/speech/batch` endpoint. Benchmark tooling may call the provider adapter directly inside the service process.

## Provider abstraction

`TTSProvider` is the only model-runtime dependency used by the scheduler.

Conceptual protocol:

```python
class TTSProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def batch_key(self, request: ProviderRequest) -> Hashable: ...
    async def synthesize(self, request: ProviderRequest) -> AudioResult: ...
    async def synthesize_batch(self, requests: Sequence[ProviderRequest]) -> Sequence[AudioResult]: ...
    async def enroll_voice(self, reference_audio: bytes, options: EnrollmentOptions) -> ProviderVoiceProfile: ...
```

`ProviderCapabilities` includes at least:

```text
provider_name
model_revision
sample_rate_hz
supports_native_batch
max_batch_size
supports_voice_cloning
supports_mixed_voice_batch
supported_styles
supported_expressive_cues
supported_response_formats
```

A future provider can implement the same contract even if `supports_native_batch=false`; the scheduler then uses effective batch capacity one and callers remain unchanged.

## VieNeu v3 Turbo provider

### Backend selection

Normal configuration uses SDK auto-selection:

```text
CUDA available + compatible PyTorch -> PyTorch/GPU
otherwise                         -> ONNX/CPU
```

Operational configuration may force CPU or GPU for verification. The service reports the selected accelerator/backend through readiness/metrics.

CPU/ONNX is a fallback/correctness path. Because upstream `infer_batch()` runs sequentially on CPU, Change T MUST NOT add a coalescing delay merely to create CPU batches. On a non-native-batch provider/backend, effective scheduler batch size is one unless a future provider explicitly advertises useful batching.

### Mixed voice/style batching

The public VieNeu `infer_batch()` helper is insufficient for the product requirement because it resolves one voice/style for all texts. The current v3 Turbo batch engine accepts per-request:

```text
speaker_emb
ref_codes
style
phonemes/text
```

Therefore `VieNeuV3TurboProvider` MAY use the SDK's lower-level `V3TurboBatchEngine.generate_batch()` to batch different voice profiles/styles together.

This is an intentional, isolated dependency on an upstream implementation surface. Requirements:

- pin exact compatible VieNeu package version/revision in the TTS lock/build;
- only `providers/vieneu_v3.py` may import/use that internal batch-engine surface;
- add startup/provider contract checks for required symbols/signatures/behavior;
- add golden contract tests for mixed preset voices, mixed cloned voices, mixed styles, output count/order, and expressive cues;
- fail readiness on incompatible SDK changes rather than silently falling back to incorrect voice routing;
- replacing VieNeu later must not alter the backend-facing HTTP contract.

### Batch compatibility key

For VieNeu mixed-voice batches, `voice_profile_id` and `style` are not necessarily part of the compatibility key because they can be represented per request by the lower-level engine.

The provider's `batch_key()` MUST include any settings that are scalar across `generate_batch()` and cannot differ safely by row, including the installed model revision and effective generation/sampling configuration. Output encoding may happen after model inference and therefore need not force a distinct model batch if the raw waveform contract is identical.

Provider logic, not the generic scheduler, owns this decision.

## Voice profile subsystem

### Tenant isolation

Every profile has:

```text
voice_profile_id
tenant_id
provider_name
provider_model_revision
profile_kind = preset | cloned
display_name
provider_payload_location
created_at
```

Cloned provider payload contains the enrolled speaker representation (for VieNeu, speaker embedding and optional reference codes). It is never addressable by display name alone.

Two tenants may both call a profile “Giọng của tôi” without collision. Authorization uses `tenant_id + voice_profile_id`, not a process-global VieNeu voice-name registry.

### Persistence

Voice enrollment is performed once. The result MUST survive process restart and MUST NOT call `add_voice()`/reference encoding on every synthesis.

`VoiceProfileStore` provides a storage abstraction. Local development may use a filesystem store. Production configuration must use restart-safe shared persistence (for example the project's existing object-storage path) so a recreated TTS task can load a tenant profile by ID. Change T does not require provisioning a new cloud storage product if an existing project store can be reused.

The service may cache decoded profiles in memory with bounded LRU behavior; the persistent store remains source of truth.

## Scheduler design

### Continuous admission, static execution

There are two separate states:

```text
PENDING
IN_FLIGHT
```

Requests may always be added to PENDING. Once a provider batch is dispatched, its membership is immutable. New arrivals wait for a later batch.

This is deliberately called **continuous dynamic micro-batching** at the service level, not true in-flight continuous batching.

### Dispatch rules

Config defaults start with:

```text
max_batch_size        = min(provider.max_batch_size, 32)
coalesce_window_ms    = 10
```

`10 ms` is an initial service default and MUST be benchmarked/tunable; it is not assumed universally optimal.

On a native-batch GPU provider:

1. If the compatible candidate set reaches `max_batch_size`, dispatch immediately.
2. If a provider batch completes and compatible requests are already pending, dispatch the next batch immediately without opening a new coalescing window.
3. If the GPU/provider is idle and the queue was previously empty, the first request opens the coalescing window. Dispatch when the window expires or the batch fills, whichever comes first.
4. Requests whose deadline would be violated by waiting must be dispatched earlier if a compatible execution slot is available.
5. Requests arriving while a batch is in flight stay pending and are considered immediately after completion.

On CPU/non-native-batch providers, the service uses effective batch size one and no coalescing wait.

### Fairness

A single 60-minute script can create hundreds of chunks, so global FIFO alone is insufficient: it can let one session occupy every batch slot.

Within each priority tier, selection uses session-aware fair queuing/round-robin semantics:

```text
Session A: A0 A1 A2 A3 ...
Session B: B0 B1 ...
Session C: C0 ...

candidate selection:
A0 B0 C0 A1 B1 A2 ...
```

Exact implementation may use deficit round-robin or equivalent deterministic fair selection, but MUST satisfy:

- no active session with eligible work can be indefinitely starved by another session at the same priority;
- per-session FIFO order is preserved at admission selection unless an explicit future protocol changes it;
- old requests age toward dispatch rather than being perpetually skipped for better batch packing;
- batching/length optimization cannot override correctness or starvation bounds.

### Priority

The public request supports a small provider-neutral priority enum, initially at least:

```text
normal
high
```

Change T defines scheduler semantics only. It does not define why a request is high priority. `/ws/platform`, viewer Q&A, script interruption, and Director policy remain future specs.

High-priority pending work is considered before normal work, but an already-running provider batch is not preempted. Normal priority must receive aging/fairness protection so sustained high-priority traffic cannot create unbounded starvation.

### Backpressure

Config owns:

```text
global_pending_limit
per_session_pending_limit
request_deadline_ms
max_batch_size
coalesce_window_ms
```

Admission beyond a bound fails fast with stable overload semantics rather than unbounded RAM/VRAM growth. Backend callers can then reduce their prefetch window/retry according to their own policy.

Backpressure is distinct from GPU batch size. A queue may contain far more requests than one batch, but only within explicit bounds.

### Cancellation/disconnect

If an HTTP caller disconnects before dispatch, the pending request is removed/cancelled. If it disconnects after dispatch, the provider run may continue because VieNeu's static batch has already been formed; the result for that request is discarded and must not be routed to another caller.

Cancellation must not invalidate sibling requests in the same provider batch.

## Request/result routing and ordering

Every provider result is zipped to immutable internal request identity, never matched only by list index after requests have been reordered across queues.

Correctness invariant:

```text
request R(session=A, utterance=U, seq=4)
    -> only audio result for R
```

A mixed batch may contain A4, B0, C7, and A5. The service may return the independent HTTP calls in any completion timing allowed by transport, but metadata/result identity MUST remain exact.

Backend/avatar session-local playback ordering is not implemented by this service. Change T gives the backend stable sequence metadata/result correspondence; backend retains its ordered AudioWindow/playback queue.

## Audio format boundary

Provider inference yields a canonical internal waveform representation. Response encoding is a separate adapter step. The external contract supports the existing required response format(s) and exposes actual sample rate/format metadata. Provider replacement must not require backend knowledge of model codec tokens or native tensor types.

VieNeu v3 Turbo currently produces 48 kHz audio; the API contract must report/encode that correctly rather than inheriting old v2 assumptions.

## Observability

Metrics/logs must be content-private by default. Record IDs and sizes, not raw text/reference audio.

Required metrics include:

- `tts_requests_total` by outcome/provider/priority;
- pending queue depth globally and per priority;
- queue wait p50/p95/p99;
- batch size and batch-fill ratio distribution;
- coalescing wait;
- provider inference wall time;
- output audio seconds;
- aggregate RTF and audio-seconds-per-wall-second;
- service overhead versus direct provider benchmark;
- active session count and fairness wait distribution;
- overload/rejected/deadline/cancelled requests;
- voice-profile cache hit/miss/enrollment time;
- selected backend (CUDA/PyTorch vs ONNX/CPU);
- GPU utilization/VRAM when the deployment exposes them.

No metric label may contain unbounded raw `session_id`, request text, or voice name. High-cardinality tracing stays in structured logs/spans with sampling.

## Benchmark and acceptance strategy

### Direct provider baseline

`benchmark_provider.py` runs VieNeu directly on the benchmark host with fixed corpus/config and records at least:

```text
batch size
item count
wall seconds
audio seconds
RTF
realtime_x
items_per_second
```

GPU sweep covers useful sizes such as 1, 4, 8, 16, and 32. CPU only requires single/compatibility smoke because upstream ONNX batch execution is sequential.

The user-provided Tesla T4 result is retained as prior evidence/reference, not a universal SLA.

### Service benchmark

`benchmark_multisession.py` drives the real HTTP service using concurrent independent sessions and ordinary `/v1/audio/speech` requests. It MUST cover:

- same preset voice across sessions;
- mixed preset voices;
- mixed cloned voices;
- mixed styles when provider engine supports them;
- burst and staggered/continuous arrival;
- 1/2/4/8/16/32 concurrent sessions or the maximum supported by the benchmark environment;
- long-running dominant session plus newly arriving short sessions;
- normal/high priority mix;
- cancellation and overload.

At saturated compatible GPU load, service throughput SHOULD retain at least 80% of direct-provider audio-seconds-per-wall-second on the same host/config. Falling below that threshold is a performance gate failure unless a measured correctness/safety requirement explains and explicitly revises the acceptance rule in OpenSpec before closeout.

Correctness gates are absolute: zero cross-session audio routing errors, zero wrong-voice profile routing, zero duplicate/missing accepted results, bounded queue state, and no starvation beyond the configured/tested fairness bound.

Low-load scheduler overhead is measured separately. The service SHOULD not add unexplained queue wait beyond the configured coalescing/deadline policy.

### Capacity report

Do not encode one global “max sessions” constant from a single GPU. Record a capacity report per hardware/provider configuration with:

```text
GPU class
provider/model revision
scheduler config
workload corpus
voice mix
concurrent sessions
queue wait percentiles
throughput
RTF
GPU/VRAM
error/overload rate
```

This allows T4, L4, A10, or future providers to be compared without changing the public architecture.

## Error handling

- Invalid request/profile/style/cue -> deterministic 4xx with stable code.
- Unauthorized tenant/profile access -> 404/403 according to existing service security convention; never leak profile existence across tenant boundary.
- Queue full/per-session prefetch excessive -> 429 (or project-standard overload code) with retry metadata if available.
- Deadline exceeded before dispatch -> 408/504-style stable domain code according to existing API convention.
- Provider/model failure -> 5xx; sibling requests are independently resolved when possible.
- Provider adapter compatibility failure at startup -> readiness false; do not silently route mixed voices through a shared-voice wrapper.

## Security and privacy

- Reference WAV and encoded voice profile are tenant data.
- Do not log raw reference audio, speaker embeddings, reference codes, or full synthesis text in normal logs.
- Validate reference media type, duration, size, and decode before enrollment.
- Scope every voice profile by tenant/account authorization.
- Deleting a cloned profile invalidates future synthesis by that ID and removes persistent provider data according to store semantics.

## Migration

1. Introduce provider-neutral API/request models and provider abstraction behind current TTS service contract.
2. Implement VieNeu v3 Turbo single-synthesis adapter and readiness.
3. Add voice-profile enrollment/persistence and preset mapping.
4. Add mixed-voice provider batch adapter with exact SDK pin/contract tests.
5. Add scheduler/admission/fairness/backpressure around provider calls.
6. Switch production synthesis route to scheduler.
7. Remove vLLM/vLLM-Omni active TTS execution and stale v2 model/entrypoint assumptions from Docker/config/docs.
8. Run direct provider, service multi-session, integration, and soak gates.
9. Freeze Change T API/capability contract for Change B integration.

Migration risk may change sequencing, but the final architecture must not retain a second active vLLM-Omni TTS path merely for convenience.

## Relationship to Change A and Change B

### Change A

Change A is complete and remains the source of speech chunks. Change T does not modify its segmentation strategy. A post-Change-T integration smoke should verify:

```text
Change A TextChunk[]
  -> ordinary TTS requests
  -> Change T scheduler/provider
  -> audio
```

This is integration verification, not reopening Change A benchmark acceptance.

### Change B

Change B authoring produces approved `spoken_text`, then uses Change A to segment it. Change B/backend may keep a bounded prefetch window of ordinary per-chunk synthesis requests, but MUST NOT:

- construct GPU batches;
- call VieNeu SDK;
- depend on speaker embeddings/reference codes;
- depend on a VieNeu-specific `/batch` endpoint.

Change T is the service-level runtime prerequisite that makes those concurrent requests efficient across all sessions.

## Decisions

### Decision: batch in `tts_service`, not backend

Accepted. The GPU-owning service has visibility across sessions and can maximize fill while enforcing global fairness/backpressure. Backend-created batches fragment capacity and leak provider mechanics.

### Decision: continuous admission around static provider batches

Accepted. Upstream VieNeu v3 Turbo batch engine is static. Change T creates vLLM-like serving behavior at the request scheduler level without falsely claiming in-flight continuous insertion.

### Decision: use mixed-voice VieNeu engine through a narrow pinned adapter

Accepted. This avoids making cloned voices permanently unbatchable and preserves unit economics, while containing upstream implementation risk to one provider module with version pinning and contract tests.

### Decision: no clone-voice surcharge assumption

Accepted as architecture. Pricing is not decided by this change. Clone voice must first be benchmarked under mixed-voice batches. Any future pricing difference should be based on measured enrollment/storage/compute cost, not an assumption that cloned voices cannot batch.

### Decision: CPU is fallback, GPU is performance target

Accepted. CPU ONNX remains functional but does not receive an unnecessary batch-size performance sweep.

### Decision: no separate prepared-script batching API/change

Accepted. Prepared scripts create request concurrency; Change T batches globally. Backend playback buffering/prefetch remains a backend concern and does not require a client-visible TTS batch protocol.
