# Change T — Final Verification & Capacity Report (2026-08-12)

## Closeout status

| Task | Result |
|---|---|
| 17.1 Unit tests (provider/voices/scheduler/fairness/priority/deadline/cancel/errors) | PASS — 318 passed (full unit+contract+integration suite; 1 pre-existing fail `test_contract_drift_tts.py` do thiếu `jwt` trong venv, không liên quan Change T) |
| 17.2 Provider contract tests trên GPU runtime (preset/clone/mixed/style/cues/order) | BLOCKED — máy dev không có CUDA+torch stack (RTX 3050 laptop hiện diện nhưng không cài torch); tests đã viết với fake SDK (21 unit + 16 batch tests pass). Cần chạy trên máy GPU thật trước khi merge production |
| 17.3 API contract/integration (readiness/enrollment/overload/cancel/multi-session isolation) | PASS — contract 30/31, integration full (routing zero cross-session qua test_runtime_api + test_soak) |
| 17.4 Benchmark gates + relative throughput | BLOCKED (GPU). Scripts sẵn sàng: `benchmark_provider.py --mode real` + `benchmark_multisession.py --base-url ...`; fake smoke PASS (provider: batch 1/4/8/16/32, RTF tính đúng; multisession: same-voice/burst/dominant zero routing errors, backpressure 429 deterministic, gate 80% warning-only) |
| 17.5 Multi-session correctness/load + soak | PASS (fake) — soak: 4 sessions × 20 chunks, cancel 1/5, pending depth về 0, active_sessions rỗng, zero cross-route |
| 17.6 Ruff/format/static + backend contract regression | PASS — ruff check clean toàn bộ src/tests/scripts; backend tests (test_tts_presets + test_voice_routes) 19 pass |
| 17.7 git diff --check + vLLM-Omni/VieNeu-v2 sweep + import audit | PASS — diff check clean; src/tts + Dockerfile + entrypoint sạch vllm serve/GPU_MEMORY_UTILIZATION/v2 model ID; import audit `V3TurboBatchEngine` chỉ trong provider adapter |
| 17.8 openspec validate --strict | PASS — "Change 'multi-session-batched-tts-runtime' is valid" (đã thêm delta headers + SHALL 80%) |
| 17.9 Capacity report | GHI DƯỚI (GPU runs pending) |
| 17.10 Mark implementation-ready | Implementation DONE; performance gate GPU chưa chạy — xem report dưới |

## Capacity report (GPU runs PENDING — máy dev không có CUDA)

| Field | Value |
|---|---|
| Hardware | Dev machine (no CUDA runtime; GPU benchmarks BLOCKED — cần máy GPU: T4/L4/A10 + torch cu126) |
| Provider / model revision | vieneu_v3 / pnnbao-ump/VieNeu-TTS-v3-Turbo (SDK `vieneu==3.2.4` pinned, wheel verified 3.2.3 surface) |
| Backend | auto (pytorch/CUDA khi có, onnx/CPU fallback) — fake smoke dùng deterministic fake provider |
| Scheduler config | max_batch_size=32 (min provider), coalesce_window_ms=10, global_pending=512, per_session_pending=64, deadline_ms=30000, aging_threshold_ms=5000 |
| Voice mix | 14 preset v3 Turbo + tenant cloned profiles (filesystem store; S3 store sẵn) |
| Concurrency | 1–32 sessions (scripts sweep sẵn; fake smoke chạy 1/2) |
| Throughput (fake smoke) | provider fake: RTF ~25x (không có nghĩa thực); multisession fake: RTF 1.1–3.9 (overhead path chỉ) — KHÔNG phải số hardware |
| Queue wait p50/p95/p99 | Ghi bởi `benchmark_multisession.py` khi chạy real |
| GPU/VRAM | metrics endpoint `/v1/audio/metrics` ghi GPU khi torch cuda available (optional, try/except) |
| Errors/overload | deterministic: 429 overload (global/per-session), 408 deadline, 502 provider, 503 not-ready — test phủ |
| Reference (historical, user-provided T4) | direct infer_batch ~1.45x RTF batch=1 → ~12.58x batch=32 — KHÔNG phải SLA |

## Known gates cho supervisor

1. GPU benchmark/contract runs (17.2/17.4/17.5-soak-real) cần máy có CUDA + torch cu126 + weights — chạy `benchmark_provider.py --mode real --batch-sizes 1,4,8,16,32` rồi `benchmark_multisession.py --base-url <svc> --sessions 1,2,4,8,16,32` (scenario same-voice → mixed → dominant → priority → backpressure), so `--compare-baseline` gate 80%.
2. `test_contract_drift_tts.py` pre-existing: cần `jwt` (pyjwt) trong venv chạy `scripts/contracts/generate.py` — regen bằng venv tts (starlette version khớp) đã xử lý contract match; drift test chỉ fail ở env thiếu jwt.
3. Dockerfile GPU build: `--build-arg WITH_CUDA=1` → torch 2.13.0+cu126 override — chưa build thử trên máy này (không GPU Docker).
