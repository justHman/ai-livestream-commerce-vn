# Tasks: Multi-Session Batched TTS Runtime

## 1. Baseline, contracts, and dependency lock

- [x] 1.1 Record the current `tts_service` active files, API contract fixtures, Docker entrypoint, provider/runtime config, and backend TTS caller assumptions before editing.
- [x] 1.2 Add/refresh contract tests for the existing backend-facing `POST /v1/audio/speech` behavior that must remain provider-neutral after the runtime migration.
- [x] 1.3 Add failing tests for `GET /health`, `GET /ready`, and `GET /v1/audio/capabilities` liveness/readiness/capability distinctions.
- [x] 1.4 Pin an exact VieNeu v3 Turbo package/revision plus compatible PyTorch/transformers/CUDA dependency set in `services/product/tts_service/pyproject.toml` and lockfile; do not use a floating upstream branch at runtime.
- [x] 1.5 Replace stale default model/runtime configuration that points to VieNeu v2/vLLM-Omni with explicit provider-neutral model/provider configuration for VieNeu v3 Turbo.
- [x] 1.6 Add configuration validation for provider, accelerator mode (`auto|cpu|gpu` or repository-equivalent), model revision, response format, scheduler bounds, and voice-profile store URI.
- [x] 1.7 Add a startup compatibility test that fails if the pinned VieNeu adapter cannot find/execute the required v3 Turbo provider surface.

## 2. Provider-neutral request/result and capability model

- [x] 2.1 Create `src/tts/providers/base.py` with the provider protocol for capabilities, batch key, single synthesis, batch synthesis, and voice enrollment.
- [x] 2.2 Create `src/tts/providers/capabilities.py` with typed provider capabilities for provider/model revision, sample rate, native batching, max batch size, voice cloning, mixed-voice batching, styles, expressive cues, and output formats.
- [x] 2.3 Create internal synthesis request/result types carrying immutable `request_id`, `session_id`, `utterance_id`, `chunk_seq`, text, `voice_profile_id`, style, priority, generation config, response format, submission time, and deadline.
- [x] 2.4 Define typed provider request/result structures that do not expose provider tensors to API/scheduler layers.
- [x] 2.5 Add tests proving provider-specific profile payloads cannot be serialized through the public synthesis schema.
- [x] 2.6 Add tests proving preset and cloned voices use the same external `voice_profile_id` field.
- [x] 2.7 Add stable domain error types for invalid capability, profile not found/unauthorized, overload, deadline, cancellation, provider unavailable, and provider inference failure.

## 3. Stable HTTP API and readiness

- [x] 3.1 Update/add `src/tts/api/schemas.py` for provider-neutral speech, capability, and voice-profile API shapes.
- [ ] 3.2 Update/add `src/tts/api/routes.py` so `POST /v1/audio/speech` creates one scheduler request and waits for exactly that request's result.
- [x] 3.3 Add response metadata/headers or structured tracing needed to preserve request/session/utterance/chunk identity without embedding raw text.
- [x] 3.4 Implement `GET /health` as process liveness only.
- [x] 3.5 Implement `GET /ready` so it is false while provider/model/profile store/scheduler startup is incomplete or adapter compatibility checks fail.
- [x] 3.6 Implement `GET /v1/audio/capabilities` from the active provider plus service-level limits; do not expose speaker embeddings/reference codes.
- [x] 3.7 Add API contract tests for valid speech, invalid text/profile/style/cue, unsupported response format, overload, deadline, provider failure, and readiness states.
- [x] 3.8 Verify no backend-facing public `/v1/audio/speech/batch` dependency is introduced; if a legacy route exists, remove it from required caller contracts or mark it non-production/internal according to final design.

## 4. Voice profile domain and persistence

- [x] 4.1 Create `src/tts/voices/models.py` with tenant-scoped opaque `VoiceProfile` metadata and `preset|cloned` profile kinds.
- [x] 4.2 Create `src/tts/voices/store.py` with `VoiceProfileStore` interface for metadata/provider payload load/save/delete/list-by-tenant operations.
- [x] 4.3 Implement a filesystem-backed profile store for local/test use with atomic writes and restart persistence.
- [x] 4.4 Implement/configure the production restart-safe shared profile store using the project's existing persistent object-storage mechanism without requiring backend access to provider payloads.
- [x] 4.5 Define serialized VieNeu profile payload containing provider model revision, speaker embedding, optional reference codes, and provider metadata with schema/version tagging.
- [x] 4.6 Add bounded in-memory LRU cache for decoded voice profiles; persistent store remains source of truth.
- [x] 4.7 Add tests for cache hit/miss/eviction and process restart reload.
- [x] 4.8 Add tenant-isolation tests proving two tenants may use the same display name while IDs/data remain distinct.
- [x] 4.9 Add delete tests proving one tenant/profile deletion does not affect unrelated profiles and future requests by deleted ID fail deterministically.

## 5. Voice enrollment API and validation

- [x] 5.1 Create `src/tts/voices/enrollment.py` to validate reference media type, decodeability, duration, channel/sample constraints, and configured byte limit before provider enrollment.
- [x] 5.2 Implement `POST /v1/voices` enrollment API with tenant authorization, reference WAV upload, display metadata, optional style/defaults, and opaque ID creation.
- [x] 5.3 Implement `GET /v1/voices/{voice_profile_id}` returning provider-neutral profile metadata only.
- [x] 5.4 Implement `DELETE /v1/voices/{voice_profile_id}` with tenant authorization and persistent/cache cleanup.
- [x] 5.5 Seed/import VieNeu preset voices as provider-owned `VoiceProfile` records or deterministic preset-profile mappings so callers use the same ID abstraction.
- [x] 5.6 Add enrollment tests for valid clean audio, provider denoise/default path, malformed audio, oversized/overlong audio, tenant collision, restart reuse, and deletion.
- [x] 5.7 Add tests proving synthesis reuses enrolled representation and does not re-encode the reference WAV for every speech chunk.

## 6. VieNeu v3 Turbo single-provider path

- [x] 6.1 Create `src/tts/providers/vieneu_v3.py` and initialize `Vieneu` with pinned v3 Turbo configuration using provider-owned lifecycle.
- [x] 6.2 Implement normal `auto` backend selection and expose actual selected backend (`pytorch/cuda` or `onnx/cpu`) through capabilities/readiness/metrics.
- [x] 6.3 Implement forced CPU and forced GPU configuration paths used by tests/operations, with clear startup failure when forced GPU prerequisites are missing.
- [x] 6.4 Implement provider profile resolution from `voice_profile_id` to VieNeu speaker embedding/reference codes without mutating a process-global voice-name registry per request.
- [x] 6.5 Implement single synthesis through the provider abstraction and canonical raw waveform/audio-result representation.
- [x] 6.6 Add expressive cue/style validation based on the pinned provider capability set, including supported laugh/sigh/throat-clear cue semantics.
- [x] 6.7 Add unit/integration tests for preset voice, cloned voice, three supported styles where applicable, expressive cues, 48 kHz metadata, and output encoding.
- [x] 6.8 Add CPU fallback smoke tests for startup, one preset synthesis, one cloned synthesis, and public API compatibility; do not add a CPU batch-size throughput sweep.

## 7. VieNeu mixed-voice static batch adapter

- [ ] 7.1 Isolate import/use of VieNeu `V3TurboBatchEngine`/internal v3 Turbo serving surface to `providers/vieneu_v3.py` (or one provider-private helper under the same package).
- [ ] 7.2 Add provider startup contract checks for the pinned engine's expected per-request `speaker_emb`, `ref_codes`, `style`, request list, output count/order, and batch-wide generation parameters.
- [ ] 7.3 Implement `batch_key()` from provider/model revision and batch-wide effective generation parameters; do not include `voice_profile_id` or per-request style when the pinned engine supports those per row.
- [ ] 7.4 Implement `synthesize_batch()` that builds one per-request VieNeu engine request with its own phonemes/text, speaker embedding, reference codes, style, and reference-code flag.
- [ ] 7.5 Validate batch result cardinality/order and map each waveform back to immutable provider request identity before returning to the scheduler.
- [ ] 7.6 Add mixed preset-voice batch tests with at least two distinct preset profiles.
- [ ] 7.7 Add mixed cloned-voice batch tests with at least two independently enrolled reference profiles.
- [ ] 7.8 Add mixed preset+cloned batch tests.
- [ ] 7.9 Add mixed-style batch tests for supported styles.
- [ ] 7.10 Add expressive-cue batch tests and verify one request's cue/style/profile cannot leak into another row.
- [ ] 7.11 Add a repository-wide import audit test/script that fails if VieNeu internal batch-engine imports appear outside the designated provider adapter/test boundary.

## 8. Scheduler data structures and admission

- [ ] 8.1 Create `src/tts/scheduler/models.py` with pending/in-flight request state, priority, effective deadline, immutable completion future, and provider batch key.
- [ ] 8.2 Create `src/tts/scheduler/admission.py` with bounded global and per-session pending accounting.
- [ ] 8.3 Implement admission validation before queue insertion, including profile/capability checks that can fail without consuming scheduler capacity.
- [ ] 8.4 Implement stable overload outcome when the global queue is full.
- [ ] 8.5 Implement stable overload outcome when one session exceeds its pending limit while preserving capacity for other sessions.
- [ ] 8.6 Implement pending-request cancellation/removal when the HTTP caller disconnects before provider dispatch.
- [ ] 8.7 Add deterministic unit tests for global/per-session bounds, admission/release accounting, duplicate request IDs, cancellation, and deadline expiration.

## 9. Fairness and priority selection

- [ ] 9.1 Create `src/tts/scheduler/fairness.py` implementing session-aware deterministic fair selection (deficit round robin or equivalent) within one priority tier.
- [ ] 9.2 Preserve per-session FIFO chunk selection for accepted requests at the same priority.
- [ ] 9.3 Add `normal` and `high` provider-neutral priority tiers without referencing `/ws/platform`, comments, Q&A, or Director concepts.
- [ ] 9.4 Implement high-before-normal selection for pending work without preempting an already-running static provider batch.
- [ ] 9.5 Implement aging/starvation protection so accepted normal work makes bounded progress under sustained high-priority arrivals.
- [ ] 9.6 Ensure batch-packing optimizations cannot skip an old eligible session indefinitely.
- [ ] 9.7 Add fairness tests with one 60-minute-equivalent deep queue plus newly arriving same-priority sessions.
- [ ] 9.8 Add priority tests for high arrival during normal in-flight batch, mixed high/normal backlog, and sustained high traffic with normal progress.

## 10. Continuous dynamic micro-batch runtime

- [ ] 10.1 Create `src/tts/scheduler/runtime.py` owning pending populations, one provider execution slot/runtime lane per compatible active provider as designed, batch dispatch, and result resolution.
- [ ] 10.2 Implement native-batch dispatch bound as `min(service_max_batch_size, provider.max_batch_size)` with default service ceiling 32.
- [ ] 10.3 Implement default `coalesce_window_ms=10` for native-batch GPU providers as a benchmark-tunable configuration value.
- [ ] 10.4 Implement idle/empty first-arrival coalescing and dispatch on window expiry.
- [ ] 10.5 Dispatch immediately when the candidate batch fills before the coalescing deadline.
- [ ] 10.6 When an in-flight batch completes and backlog exists, dispatch the next eligible batch immediately without an additional idle coalescing wait.
- [ ] 10.7 Keep requests arriving during provider inference pending for the next batch; never mutate membership of an in-flight static VieNeu batch.
- [ ] 10.8 Dispatch early when waiting the full coalescing window would violate an accepted request deadline and capacity is available.
- [ ] 10.9 On CPU/non-native-batch providers, force effective batch size one and zero throughput coalescing delay.
- [ ] 10.10 Resolve each completion future exactly once and discard a cancelled-after-dispatch result without affecting sibling batch members.
- [ ] 10.11 Add fake-provider deterministic-clock tests for every dispatch rule, deadline edge, backlog transition, and result mapping.

## 11. API-to-scheduler integration and failure isolation

- [ ] 11.1 Wire speech HTTP route -> admission -> scheduler -> provider -> audio encoder -> same request response.
- [ ] 11.2 Add request-context propagation for trace/log correlation without using raw text or high-cardinality IDs as metric labels.
- [ ] 11.3 Implement provider batch failure mapping so affected requests fail deterministically and later queued work can continue if provider readiness remains healthy.
- [ ] 11.4 Ensure one invalid request is rejected before batch construction rather than poisoning valid compatible siblings.
- [ ] 11.5 Ensure caller disconnect after dispatch only marks/discards that caller's result and does not cancel sibling requests in the static provider batch.
- [ ] 11.6 Add mixed-session integration tests where provider completion order/batch membership differs from submission groups; assert zero cross-route, duplicate, or missing successful results.
- [ ] 11.7 Add same-session concurrent-chunk tests proving response identity retains `utterance_id/chunk_seq` even when chunks land in different batches.

## 12. Observability and operational controls

- [ ] 12.1 Add counters for admitted/completed/rejected/deadline/cancelled/provider-failed requests by bounded provider/backend/priority/outcome labels.
- [ ] 12.2 Add gauges/histograms for global pending depth, priority depth, active-session count, queue wait, and coalescing wait.
- [ ] 12.3 Add batch metrics for size, fill ratio, provider inference wall time, generated audio seconds, RTF/realtime factor, and audio-seconds-per-wall-second.
- [ ] 12.4 Add voice-profile enrollment/cache metrics without profile IDs as unbounded metric labels.
- [ ] 12.5 Expose selected provider/model/backend and scheduler limits through readiness/capabilities and startup logs.
- [ ] 12.6 Add optional GPU utilization/VRAM collection when the deployment exposes metrics; absence of GPU metrics must not break synthesis.
- [ ] 12.7 Add structured sampled tracing with request/session IDs while keeping normal logs free of full text, reference audio, embeddings, and reference codes.
- [ ] 12.8 Add tests scanning normal logs/metrics payloads for accidental raw synthesis text or provider voice payload leakage.

## 13. Docker/runtime migration away from vLLM-Omni

- [ ] 13.1 Rewrite TTS Docker dependencies/entrypoint to start the Python `tts_service` application with pinned VieNeu v3 Turbo runtime instead of `vllm serve`/Omni.
- [ ] 13.2 Remove active `MODEL_ID=pnnbao-ump/VieNeu-TTS-v2` defaults and other v2-only startup assumptions; document current v3 Turbo model/provider configuration.
- [ ] 13.3 Remove active `GPU_MEMORY_UTILIZATION`/Omni-specific options that have no meaning in the new provider runtime, or rename/redefine resource controls explicitly for the new service.
- [ ] 13.4 Keep NVIDIA device exposure/container requirements needed by VieNeu GPU/PyTorch and verify the service also boots in CPU-only local/test mode.
- [ ] 13.5 Update `services/product/tts_service/README.md` to describe provider-neutral serving, port/health/readiness, VieNeu v3 Turbo default provider, voice profiles, scheduler, and benchmark commands.
- [ ] 13.6 Run repository-wide search for active TTS `vllm-omni`, `vllm serve`, VieNeu-v2 model ID, and stale serving-entrypoint notes; retain only genuinely historical documentation where clearly labeled.

## 14. Direct provider benchmark

- [ ] 14.1 Add `scripts/benchmark_provider.py` with fixed corpus/config recording backend, batch size, items, wall seconds, audio seconds, RTF, realtime factor, and items/sec.
- [ ] 14.2 Run GPU direct baseline for batch sizes 1, 4, 8, 16, and 32 where supported on the target benchmark GPU.
- [ ] 14.3 Add same-voice direct provider corpus and mixed-voice/mixed-cloned provider corpus using the adapter path actually used by service scheduling.
- [ ] 14.4 Record the user-provided Tesla T4 benchmark as historical/reference evidence alongside new reproducible runs, without turning its exact numbers into a hardware-independent SLA.
- [ ] 14.5 Run CPU ONNX single/compatibility smoke and record fallback RTF; do not spend time on a CPU batch-size sweep unless upstream capability changes.
- [ ] 14.6 Store benchmark config/provider revision/hardware metadata with results so runs are comparable.

## 15. Multi-session service benchmark and load tests

- [ ] 15.1 Add `scripts/benchmark_multisession.py` that uses only ordinary concurrent `/v1/audio/speech` calls rather than a client batch endpoint.
- [ ] 15.2 Implement session-count sweep for 1, 2, 4, 8, 16, and 32 concurrent sessions when supported by the host.
- [ ] 15.3 Benchmark same preset voice across many sessions to measure ideal batch fill/service overhead.
- [ ] 15.4 Benchmark mixed preset voices across sessions and verify no profile routing errors.
- [ ] 15.5 Benchmark mixed cloned voices across tenants/sessions and verify they share provider batches when generation settings are compatible.
- [ ] 15.6 Benchmark mixed reading styles where the pinned VieNeu engine supports per-request style.
- [ ] 15.7 Benchmark burst arrival where many sessions submit nearly simultaneously.
- [ ] 15.8 Benchmark continuous staggered arrival while provider batches remain active; verify pending backlog immediately feeds successive batches.
- [ ] 15.9 Benchmark a dominant long-script session plus newly arriving sessions; record fairness wait and assert no indefinite starvation.
- [ ] 15.10 Benchmark high/normal priority mix without introducing `/ws/platform` semantics.
- [ ] 15.11 Benchmark per-session/global backpressure and verify deterministic overload rather than memory growth.
- [ ] 15.12 Benchmark pending cancellation and cancelled-after-dispatch behavior.
- [ ] 15.13 Compare saturated service audio-seconds-per-wall-second with direct-provider baseline on identical host/corpus/config and enforce the 80% relative-throughput gate.
- [ ] 15.14 Record queue wait p50/p95/p99, batch size/fill distribution, inference latency, aggregate RTF, errors, cancellations, GPU/VRAM when available, and active session count.

## 16. Soak, Change A integration, and Change B contract freeze

- [ ] 16.1 Add a long-running multi-session soak scenario with continuous arrival, mixed sessions/voices, cancellations, and bounded queues.
- [ ] 16.2 Assert queue depth returns to baseline after soak and RAM/VRAM do not show unexplained unbounded growth.
- [ ] 16.3 Add Change A integration smoke: canonical `TextChunk[]` -> bounded concurrent ordinary TTS requests -> Change T -> audio; do not modify TextChunker acceptance or create a client batch API.
- [ ] 16.4 Verify backend integration contains no VieNeu imports, speaker embedding/reference code handling, or provider batch construction.
- [ ] 16.5 Freeze/document the provider-neutral Change T contract consumed by Change B: `voice_profile_id`, capabilities, priority, per-chunk synthesis, readiness/error semantics.
- [ ] 16.6 Update Change B dependency/reference docs if needed so production integration is blocked on Change T runtime acceptance but authoring logic remains provider-neutral.
- [ ] 16.7 Verify `/ws/platform`, viewer Q&A, Director interruption, and semantic priority policy remain absent from Change T implementation/spec scope.

## 17. Final verification and closeout

- [ ] 17.1 Run all TTS unit tests for provider abstractions, voice profiles, scheduler dispatch, fairness, priority, deadlines, cancellation, and error mapping.
- [ ] 17.2 Run provider contract tests on pinned VieNeu GPU runtime for preset, clone, mixed voice, mixed style, expressive cues, and output order/count.
- [ ] 17.3 Run API contract/integration tests including readiness, enrollment, overload, cancellation, and multi-session result isolation.
- [ ] 17.4 Run direct-provider and service benchmark gates and record relative service throughput against direct provider.
- [ ] 17.5 Run multi-session correctness/load matrix and soak test; zero cross-session/wrong-voice/duplicate/missing accepted-result failures are mandatory.
- [ ] 17.6 Run the repository's TTS-service Ruff/format/type/static checks and relevant backend contract regression tests.
- [ ] 17.7 Run `git diff --check` and repository-wide architecture searches for stale active vLLM-Omni/VieNeu-v2 TTS paths and leaked VieNeu internal-engine imports.
- [ ] 17.8 Run `openspec validate multi-session-batched-tts-runtime` in strict repository mode and fix every validation error before completion.
- [ ] 17.9 Record final capacity report with hardware, provider/model revision, scheduler configuration, voice mix, concurrency, throughput, queue wait, GPU/VRAM, and overload/error results.
- [ ] 17.10 Mark Change T implementation-ready/complete only after provider-neutral API, mixed cloned-voice batching, fairness/backpressure, performance gate, multi-session correctness, and strict OpenSpec validation all pass.
