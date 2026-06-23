# Self-host diffusion avatar renderer — research (2026-06-22)

For the future `core/render/self_host.py` (replaces LiveAvatar cloud). Use case:
"batch streaming" — generate ~10s video batches, play batch N while generating N+1
(throughput-bound `gen_time <= play_time`, NOT frame-latency-bound). All repos
verified on HF Hub / GitHub. RTF figures for 14B models are inferred, NOT
benchmarked — measure on real hardware before committing.

## Key facts
- **lip-sync is language-agnostic** (audio waveform/embedding driven) — Vietnamese
  needs no model finetune; quality depends on clean audio, not language.
- Batch-streaming (throughput-bound) is EASIER than LiveAvatar cloud's frame-latency
  target — an offline-batch model with RTF≈1 works in a producer-consumer pipeline.
- Hard tradeoff: the right architecture (autoregressive + built-in anti-drift, 14B)
  needs >40GB VRAM; models light enough for one 4090/A100-40GB lack autoregressive
  anti-drift (identity drifts when chaining batches).

## Ranked (commercial-OK + multi-ref + 1-2 GPU)

────
#1 Live Avatar ── Quark-Vision/Live-Avatar (HF) + Alibaba-Quark/LiveAvatar (GitHub), Apache-2.0
   14B. multi-ref ✅, autoregressive infinite (>10000s) ✅, anti-drift built-in ✅.
   THIS IS the open-source release of the LiveAvatar cloud we currently rent.
   VRAM: >=48GB (FP8 v1.1) single-GPU offline, or multi-H800 for 45 FPS realtime.
   On our HW: needs ~2xA100-40GB pooled for offline-batch; RTF on A100 UNVERIFIED.
   arXiv 2512.04677. Base = Wan2.2-S2V-14B. Migration from cloud is most direct.
#2 Ditto ──── digital-avatar/ditto-talkinghead, Apache-2.0 (code). ~0.2B.
   Stream-optimized, audio-driven, RTF<1 on 1x4090, ~4-8GB. single-ref, weaker
   anti-drift, lower fidelity. Safe throughput choice on one GPU.
#3 MuseTalk ── TMElyralab/MuseTalk, CreativeML-OpenRAIL-M (use-restrictions ⚠️).
   ~0.4B, real-time 30+FPS, 5-8GB. Lip-sync onto an EXISTING video/face crop,
   not full generation from one image.
backup EchoMimicV3 ── BadToBest/EchoMimicV3, Apache-2.0, 1.3B, ~16GB. Light; RTF may
   still be >1 on 4090 — measure.
────

## Excluded
- **API-only / no weights (cannot self-host):** OmniHuman-1 (ByteDance), EMO/EMO2
  (Alibaba), Loopy (ByteDance).
- **Non-commercial / unclear license:** MuseTalk/MuseV (OpenRAIL-M), LatentSync
  (OpenRAIL++), EchoMimic v1 (no LICENSE), Sonic (no explicit license),
  HunyuanVideo-Avatar (Tencent license, regional/usage limits).
- **Wrong modality (video-driven, not audio):** LivePortrait, SkyReels-A1 — would
  need an extra audio→motion stage.

## Decision
- Have 2xA100 / a 48GB+ GPU → **Live Avatar** (same family as the cloud; multi-image
  anti-drift; batch-streaming offline is fine since we're throughput-bound).
- One GPU, need guaranteed throughput now → **Ditto** (accept quality/anti-drift tradeoff).
- Plug into `core/render/self_host.py` behind the existing RenderBackend seam — no
  change to `core/api` or the Director.

## Must-verify before commit
- Benchmark RTF for any 14B model on the ACTUAL GPU (A100/4090) — no public A100
  numbers exist; the "fails gen<=play" calls for 14B are inferred from denoise cost.
- Live Avatar on 2xA100: TPP wants 4-5 GPUs for 45 FPS realtime; 2 GPUs + FP8 is
  likely offline-batch only (acceptable for our pipeline, but the #1 risk to measure).
- Confirm exact param counts + licenses (legal review) for any shortlisted model.
