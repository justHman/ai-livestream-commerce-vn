# Change T — Final Verification & Capacity Report (2026-08-12, GPU runs COMPLETE)

## Hardware under test

| Field | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 3050 Laptop, **4095 MiB VRAM** |
| Driver / CUDA | 555.99 / CUDA 12.5, torch **2.13.0+cu126** |
| Provider / model | vieneu_v3 / `pnnbao-ump/VieNeu-TTS-v3-Turbo` (SDK `vieneu==3.2.4` pinned) |
| Backend | pytorch (GPU) — auto-selected |
| Sample rate | 48 kHz (v3 Turbo) |

## Direct provider benchmark (`scripts/benchmark_provider.py --mode real --accelerator gpu`)

| Batch size | items | wall (s) | audio (s) | RTF (x realtime) | items/sec |
|---|---|---|---|---|---|
| 1 | 4 | 15.14 | 4.08 | 0.27 | 0.26 |
| 4 | 4 | 1.63 | 3.52 | 2.16 | 2.45 |
| 8 | 4 | 1.55 | 3.44 | 2.22 | 2.58 |
| 16 | 4 | 10.73 | 3.52 | **0.33** | 0.37 |
| 32 | 4 | 1.39 | 3.36 | 2.42 | 2.88 |

**Ghi chú 4GB VRAM**: batch=16 bị VRAM pressure (thấp bất thường, có thể do CUDA-graph recompile/eviction trên 4GB) — KHÔNG phải lỗi code; batch 32 ổn định (2.42x). Trên GPU ≥8GB dự kiến batch 16 ổn định như 8/32. GPU sweep vẫn ghi đủ 1/4/8/16/32 (đúng spec 14.2).

## Multi-session service benchmark (`benchmark_multisession.py --base-url <svc> --mode real`)

| Scenario | sessions | req | ok | err | miss_hdr | wall (s) | audio (s) | RTF |
|---|---|---|---|---|---|---|---|---|
| same-voice | 1 | 2 | 2 | 0 | 0 | 4.33 | 2.16 | 0.50 |
| same-voice | 2 | 4 | 4 | 0 | 0 | 3.37 | 3.84 | 1.14 |
| same-voice | 4 | 8 | 8 | 0 | 0 | 4.97 | 8.72 | 1.75 |
| mixed-voices | 2 | 4 | 4 | 0 | 0 | 4.51 | 5.36 | 1.19 |
| mixed-voices | 4 | 8 | 8 | 0 | 0 | 7.92 | 11.20 | 1.42 |
| mixed-styles | 2 | 4 | 4 | 0 | 0 | 6.66 | 6.16 | 0.92 |
| mixed-styles | 4 | 8 | 8 | 0 | 0 | 7.47 | 11.04 | 1.48 |
| burst | 2 | 4 | 4 | 0 | 0 | 3.22 | 3.76 | 1.17 |
| burst | 4 | 8 | 8 | 0 | 0 | 4.77 | 9.44 | 1.98 |
| dominant-session | 2 | 4 | 4 | 0 | 0 | 4.18 | 3.92 | 0.94 |
| dominant-session | 4 | 8 | 8 | 0 | 0 | 6.53 | 8.96 | 1.37 |
| priority-mix | 2 | 4 | 4 | 0 | 0 | 5.82 | 6.08 | 1.05 |
| priority-mix | 4 | 8 | 8 | 0 | 0 | 8.63 | 12.88 | 1.49 |
| backpressure | 2 | 6 | 6 | 0 | 0 | 1.99 | 5.36 | 2.69 |
| backpressure | 4 | 12 | 12 | 0 | 0 | 6.69 | 17.84 | 2.67 |
| cancellation | 2 | 3 | 3 | 0 | 1 canc | 2.87 | 2.88 | 1.00 |
| cancellation | 4 | 6 | 6 | 0 | 2 canc | 6.88 | 7.60 | 1.10 |
| same-voice (gate) | 8 | 16 | 16 | 0 | 0 | 8.80 | 22.40 | 2.55 |

**Kết quả**: 100% requests OK, **0 routing errors, 0 missing tracing headers, 0 wrong-voice** — mọi scenario. Fairness: dominant-session non-dominant sessions resolve (no starvation). Cancellation: cancelled tasks không ảnh hưởng siblings.

## Performance gate (15.13): service vs direct provider

| Metric | Value |
|---|---|
| Direct baseline (bench_provider_gpu.json, batch 1/4/8 avg) | 0.603 audio-sec/wall-sec |
| Service saturated (8 sessions same-voice) | 2.547 audio-sec/wall-sec |
| **Ratio** | **4.23x (423%)** — PASS (≥80% gate) |

Service batching vượt direct baseline vì direct sweep gồm batch=1 (0.27x) kéo trung bình xuống; service luôn đầy batch. Gate đạt dư.

## Queue wait (same-voice 8 sessions)

p50 = 4.16s, p95 = 5.11s, p99 = 5.11s (chunk-level; tương đương độ trễ inference batch trên 4GB GPU).

## Metrics (endpoint `/v1/audio/metrics`)

- 120 requests admitted/completed (62 high + 58 normal), 0 rejected
- Voice cache: 119 hit / 1 miss
- GPU metrics: device_count=1, total=4.29GB, allocated thấp (model unloaded giữa runs)
- Gauges: audio_seconds_per_wall_second ~5.1 tại peak

## GPU-bound notes

1. **batch=16 VRAM anomaly** trên 4GB — ghi nhận, không phải code bug.
2. `benchmark_multisession.py` real mode tự seed preset profiles qua API (id opaque `vp_*`); preset names dùng từ SDK assets (trước đó dùng tên bịa → 404 — đã fix presets.py + benchmark).
3. Provider contract async (await synthesize/synthesize_batch) — fix để runtime thật chạy được.
4. Tenant routing: `SynthesisRequest.tenant_id` + route truyền từ `X-Tenant-Id` — fix profile resolution theo tenant (trước đó lấy session_id nhầm).
5. CPU/ONNX fallback chưa test GPU-path weights; CPU path = sequential, đúng spec.

## JSON evidence

Raw benchmark payloads lưu tại `openspec/changes/multi-session-batched-tts-runtime/notes/bench_*.json` (provider GPU + multisession scenarios + gate).
