## Why

The current `tts_service` deployment contract is still centered on VieNeu-TTS v2 through a vLLM/vLLM-Omni serving path, while the project direction has changed to the VieNeu Python SDK and v3 Turbo. More importantly, the product is no longer a single-session TTS workload: multiple livestream sessions can request speech concurrently, and prepared scripts can keep several speech chunks available ahead of avatar playback. Treating every request as isolated single-item inference wastes the GPU throughput demonstrated by VieNeu v3 Turbo batching.

The required runtime is therefore not “backend creates a batch and sends it to TTS”. Batching is a serving concern owned globally by `tts_service`. Backends submit normal per-chunk synthesis requests. The TTS service continuously admits requests from all active sessions, applies fairness/priority/backpressure, forms compatible GPU micro-batches, invokes the current provider, and resolves every HTTP request with the correct audio result. This lets one GPU serve many sessions while keeping batching transparent to backend callers.

VieNeu v3 Turbo is the first provider, not the public contract. The public VieNeu `infer_batch()` helper currently resolves one voice/style for all texts in a call, but the underlying v3 Turbo batch engine accepts per-request speaker embedding, reference codes, and style. The service may use that lower-level provider engine inside one narrowly isolated adapter so preset voices and tenant-scoped cloned voices can share GPU batches when generation settings are compatible. Because that engine is not the high-level stable SDK surface, the project MUST pin the VieNeu SDK revision/version and protect the adapter with provider contract tests.

The user-provided Tesla T4 benchmark already shows the value of GPU batching: direct VieNeu GPU `infer_batch` improved from roughly 1.45x realtime at batch=1 to roughly 12.58x realtime at batch=32. Change T must preserve most of the direct provider throughput while adding multi-session scheduling correctness, fairness, queueing, cancellation, observability, and provider portability.

Change A `adaptive-speech-text-chunking` is considered complete and is not reopened by this change. Change T becomes the next runtime prerequisite before Change B `approved-script-authoring-pipeline` is integrated into production speech delivery.

## What Changes

- Replace the current vLLM/vLLM-Omni-centered TTS runtime with a **provider-neutral in-process serving runtime** inside `services/product/tts_service`.
- Keep the backend-facing synthesis contract as ordinary per-chunk HTTP requests. Backend callers MUST NOT need to know provider batch size, CUDA, VieNeu internals, or a `/batch` endpoint.
- Add a global **continuous-admission dynamic micro-batch scheduler** in `tts_service`:
  - requests may arrive continuously from many sessions;
  - pending requests are appended while a GPU batch is in flight;
  - when the current batch completes, the scheduler immediately dispatches the next compatible pending batch;
  - new requests are never inserted into an already-running VieNeu static batch;
  - at low load, a configurable short coalescing window allows compatible requests to accumulate before dispatch.
- Add a provider-neutral `TTSProvider` contract and a first `VieNeuV3TurboProvider` implementation.
- Pin VieNeu v3 Turbo SDK/model compatibility used by production and remove vLLM-Omni from the active TTS execution path.
- Use VieNeu auto backend selection for normal startup, preferring GPU/PyTorch when CUDA is available while retaining ONNX/CPU as a functional fallback/smoke path. Performance tuning and capacity acceptance focus on GPU batching; CPU `infer_batch()` is treated as sequential compatibility rather than a throughput optimization.
- Support provider capabilities such as preset voices, cloned voices, expressive cues, styles, supported output formats, and batching through a capability descriptor rather than leaking VieNeu-specific behavior into backend code.
- Add tenant-scoped **voice profiles** with opaque `voice_profile_id` values. Backend callers refer to a voice profile; provider-specific speaker embeddings/reference codes remain inside `tts_service`.
- Add voice enrollment for VieNeu v3 Turbo using a reference WAV, persist the resulting provider profile, and reuse it without re-enrolling on every synthesis request.
- Support **mixed preset/cloned voices and styles within one VieNeu GPU batch** when provider-level generation settings are compatible. This MUST be implemented only inside the pinned VieNeu adapter; private/internal SDK access must not spread through the service.
- Add provider-independent scheduling metadata to synthesis requests:
  - `session_id` for fairness/accounting;
  - opaque request/chunk identity for tracing;
  - `priority` with a default normal tier.
  Change T does not define `/ws/platform`, viewer Q&A, interruption semantics, or which future product feature receives high priority.
- Add fair scheduling across sessions so a long prepared script from one session cannot monopolize all GPU batch slots while other sessions wait.
- Add bounded global and per-session queues, overload behavior, request deadlines, cancellation/disconnect handling, and failure isolation.
- Preserve request/audio identity across batching: every caller receives only its own synthesized audio; cross-session routing errors are correctness failures.
- Preserve session-local order at the consumer boundary through stable `session_id`/utterance/chunk metadata, while allowing TTS execution itself to batch and complete requests independently.
- Add scheduler/provider observability: queue wait, batch fill, batch size distribution, inference wall time, generated audio duration, aggregate RTF/realtime factor, provider latency, GPU utilization/VRAM when available, per-session fairness, cancellation, overload, and failure counters.
- Add direct-provider benchmark tooling and service-level multi-session benchmark/load tests. Service throughput under saturated compatible load MUST be compared against direct VieNeu batching on the same host so service overhead is measurable rather than hidden by hardware differences.
- Add multi-session correctness, mixed-voice, fairness, burst, continuous-arrival, backpressure, cancellation, failure-isolation, and soak tests.
- Keep prepared-script buffering/playback policy outside this change. Backend/runtime may issue a bounded number of concurrent per-chunk requests; `tts_service` owns global GPU batching. No separate `prepared-script-batch-playback` change is required merely to obtain batching.
- Keep `/ws/platform` Q&A workflow and future priority-preemption behavior out of scope; Change T only provides a generic priority input and fair scheduler semantics.

## Capabilities

### New Capabilities

- `multi-session-batched-tts-runtime`: Provider-neutral TTS serving with transparent cross-session dynamic micro-batching, VieNeu v3 Turbo GPU execution, tenant-scoped voice profiles/cloning, fairness, priority, backpressure, observability, and multi-session load verification.

### Modified Capabilities

- Existing `tts_service` runtime contract is migrated from the vLLM/vLLM-Omni/VieNeu-v2 execution model to the new provider-neutral service implementation while preserving the backend-facing single-synthesis integration surface expected by callers.

## Dependency and Sequencing

This is **Change T**.

- Change A `adaptive-speech-text-chunking` is already complete. Change T does not reopen Change A benchmark acceptance.
- Change T SHOULD branch from the current canonical `main` that contains the completed Change A state. If Change A is complete but not yet merged, merge/land Change A first rather than branching Change T from an unmerged feature branch.
- Change B `approved-script-authoring-pipeline` may keep its proposal/design work, but production implementation/integration MUST use the stable Change T TTS contract and MUST NOT depend directly on VieNeu SDK details.
- After Change T passes its runtime/provider/load gates, Change B can send ordinary per-chunk synthesis requests after Change A segmentation. The TTS service, not Change B or backend orchestration, decides how those requests are batched on GPU.

## Impact

- **TTS service**: replace active vLLM/vLLM-Omni execution with provider abstraction, VieNeu v3 Turbo adapter, voice-profile subsystem, global scheduler, queue/backpressure, and richer observability under `services/product/tts_service/src/tts/`.
- **TTS API/contract**: retain a stable single-synthesis HTTP surface (`POST /v1/audio/speech`) and health/readiness endpoints; add provider-neutral voice-profile enrollment/management and capability discovery as needed. Do not require backend callers to use a public `/batch` endpoint.
- **Backend service**: only contract-level updates required: send stable session/chunk metadata, opaque `voice_profile_id`, optional provider-neutral style/cue options, and bounded concurrent per-chunk requests. Backend MUST NOT implement GPU batching.
- **Voice cloning**: move from process-global VieNeu voice names to tenant-scoped opaque voice profiles; persist provider-specific encoded data and protect tenant isolation.
- **Deployment**: remove vLLM-Omni runtime dependency from the TTS container; install/pin VieNeu v3 Turbo + compatible CUDA/PyTorch packages for GPU builds while retaining ONNX CPU fallback support.
- **Observability**: add scheduler/provider metrics and load-test reports needed to establish safe concurrent-session capacity on each GPU class.
- **Tests**: add unit/provider/contract/integration/load/soak coverage for mixed sessions and mixed voices.
- **Out of scope**: `/ws/platform` viewer-message workflow, Director Q&A selection, semantic priority assignment, avatar interruption/barge-in, backend script authoring, TextChunker behavior changes, true frame/token-level in-flight continuous batching inside VieNeu, arbitrary provider failover, or new general agent infrastructure.
