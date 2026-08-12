"""Direct provider throughput benchmark (Change T tasks 14.1-14.6).

Measures synthesis throughput WITHOUT HTTP: builds the provider directly and
drives ``synthesize``/``synthesize_batch`` with a fixed Vietnamese corpus.
Results are recorded per (mode, batch_size) as wall seconds, audio seconds,
RTF, realtime_x, and items/sec, with full config/hardware metadata (14.6).

Modes:
- ``--mode fake`` (default): deterministic in-process fake provider. No
  model, no SDK — safe for CI smoke and for validating the measurement path.
  ``--no-sleep`` disables the simulated inference delay for fast runs.
- ``--mode real``: the pinned ``VieNeuV3TurboProvider`` over the real SDK.
  Requires the ``vieneu`` wheel + downloadable model weights (GPU box). On
  this dev machine the model cannot run (offline, no torch/CUDA stack) — the
  script fails loudly with a hint instead of crashing (exit nonzero).
  GPU sweep: batch sizes 1/4/8/16/32 on the pytorch backend; CPU mode runs
  batch 1 only (14.5 — no CPU batch sweep until upstream capability changes).

The T4 reference numbers (14.4) are printed as historical evidence only when
``--reference`` is passed; they are never an SLA.

Usage:
    python scripts/benchmark_provider.py --mode fake --batch-sizes 1,4,8 --no-sleep
    python scripts/benchmark_provider.py --mode fake --output results.json
    python scripts/benchmark_provider.py --mode real --output results.json
    python scripts/benchmark_provider.py --mode real --accelerator gpu --reference
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from tts.config import RuntimeConfig, load_runtime_config
from tts.providers.models import (
    AudioResult,
    GenerationConfig,
    ProviderRequest,
    ProviderResult,
    SynthesisRequest,
)
from tts.providers.vieneu_v3 import SAMPLE_RATE_HZ, VieNeuV3TurboProvider

# Fixed Vietnamese corpus (14.1): short/medium/long sentences, cycled by
# index so every request in a batch draws a deterministic text.
CORPUS: tuple[str, ...] = (
    "Xin chào",
    "Cảm ơn bạn",
    "Chào buổi sáng",
    "Hôm nay trời đẹp",
    "Sản phẩm này đang giảm giá",
    "Mời quý khách xem thêm",
    "Chúng tôi giao hàng toàn quốc",
    "Thanh toán khi nhận hàng",
    "Đừng quên nhấn nút theo dõi kênh",
    "Khuyến mãi chỉ áp dụng hôm nay",
    "Chiếc áo này chất liệu cotton cao cấp, thoáng mát",
    "Số lượng có hạn, quý khách đặt hàng ngay để không bỏ lỡ",
    "Chương trình ưu đãi đặc biệt dành riêng cho khách hàng mới",
    "Đội ngũ hỗ trợ của chúng tôi luôn sẵn sàng phục vụ quý khách 24/7",
    "Mua kèm combo hai sản phẩm sẽ được giảm thêm mười phần trăm",
    "Đây là sản phẩm bán chạy nhất tháng với hơn mười nghìn đơn đã bán",
    "Vui lòng để lại số điện thoại, nhân viên sẽ liên hệ tư vấn trong vài phút",
    "Khi nhận hàng quý khách có thể kiểm tra kỹ sản phẩm trước khi thanh toán",
    "Quà tặng kèm bao gồm túi giấy cao cấp và thiệp cảm ơn cho mọi đơn hàng",
    "Chương trình khuyến mãi kết thúc vào cuối tuần này, nhanh tay đặt hàng ngay",
)

# Historical T4 reference evidence (14.4) — user-provided, machine-bound
# (Tesla T4, v3 Turbo), NEVER a hardware-independent SLA.
_T4_REFERENCE_NOTE = (
    "reference(Tesla T4, user-provided, historical): realtime_x ~1.45 at batch=1, "
    "~12.58 at batch=32 — evidence only, not an SLA"
)

DEFAULT_BATCH_SIZES = (1, 4, 8, 16, 32)


def _request(seq: int, text: str, voice_profile_id: str = "default") -> SynthesisRequest:
    return SynthesisRequest(
        request_id=f"bm-{seq}",
        session_id=f"bm-session-{seq % 3}",
        utterance_id=f"bm-utt-{seq}",
        chunk_seq=seq,
        input_text=text,
        voice_profile_id=voice_profile_id,
        response_format="wav",
        generation_config=GenerationConfig(speed=1.0),
    )


def hardware_metadata() -> dict:
    """Python platform + optional torch/CUDA facts; never raises."""
    meta = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        import torch

        meta["torch"] = torch.__version__
        meta["cuda_available"] = bool(torch.cuda.is_available())
        if torch.cuda.is_available():
            meta["cuda_device_count"] = torch.cuda.device_count()
            meta["cuda_device"] = torch.cuda.get_device_name(0)
    except Exception:
        meta["torch"] = None
    return meta


# ── fake provider ─────────────────────────────────────────────────────────────
class FakeProvider:
    """Deterministic provider: waveform length scales with text length.

    ``synthesize_batch`` preserves input order and simulates batch-sized
    inference latency (longer sleep for larger batches) so wall-time
    measurement exercises the real path shape. ``--no-sleep`` zeroes the
    delay for CI-speed smoke runs.
    """

    provider_name = "fake"

    def __init__(self, *, sample_rate: int = SAMPLE_RATE_HZ, no_sleep: bool = False) -> None:
        self.sample_rate = sample_rate
        self.no_sleep = no_sleep
        self.batch_calls = 0
        self.batched_items = 0

    def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        self.batch_calls += 1
        self.batched_items += len(requests)
        if not self.no_sleep:
            # ~1.5 ms per item + batch ramp: batch 32 ≈ 0.16 s per dispatch.
            time.sleep(0.0015 * len(requests) * (1 + len(requests) / 64))
        return [self._result(request) for request in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        # ~0.5 s of audio per ~70 chars of text; the ratio drives RTF > 1 in
        # fake mode so the metric math is visibly exercised.
        duration_ms = int(len(request.input_text) * 7.2)
        return AudioResult(
            request_id=request.request_id,
            sample_rate=self.sample_rate,
            waveform=np.zeros(self.sample_rate * duration_ms // 1000, dtype=np.float32),
            response_format=request.response_format,
            duration_ms=duration_ms,
        )


class AsyncFakeProvider(FakeProvider):
    """Async wrapper over the fake provider for the scheduler-shaped path.

    The scheduler runtime awaits ``synthesize_batch``; the real provider is
    sync, so the benchmark wraps it (mirroring how a production wiring would
    offload CPU-bound synthesis). The fake stays sync for timing honesty.
    """

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, super().synthesize_batch, requests)


class AsyncRealProvider:
    """Async adapter over the sync real provider (mirrors runtime usage)."""

    def __init__(self, provider: VieNeuV3TurboProvider) -> None:
        self._provider = provider

    @property
    def backend(self) -> str:
        return self._provider.backend

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._provider.synthesize_batch, requests)


def _build_real_provider(accelerator: str, model_revision: str) -> VieNeuV3TurboProvider:
    """Build the real provider; any failure raises with a clear hint."""
    try:
        from tts.providers.vieneu_v3 import VieNeuV3TurboProvider  # noqa: F811

        config = load_runtime_config()
        config = RuntimeConfig(
            provider="vieneu_v3",
            accelerator=accelerator,
            model_revision=model_revision or config.model_revision,
        )
        return VieNeuV3TurboProvider(config)
    except Exception as exc:
        print(
            f"ERROR: real provider initialization failed: {exc}\n"
            "hint: benchmark --mode real needs the vieneu wheel AND downloadable "
            "model weights (GPU box). This dev machine cannot run the model "
            "(offline, no torch/CUDA stack). Use --mode fake for CI smoke.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _run_sweep(
    provider,
    *,
    batch_size: int,
    samples: int,
    corpus: tuple[str, ...],
    voice_profile_id: str,
) -> list[dict]:
    """Run one (batch_size, voice) cell; returns the recorded metrics."""
    requests = [
        _request(seq, corpus[seq % len(corpus)], voice_profile_id) for seq in range(samples)
    ]
    started = time.monotonic()
    # Chunk into provider batches and dispatch sequentially — exactly how the
    # scheduler drives a saturated lane.
    results: list[ProviderResult] = []
    for offset in range(0, len(requests), batch_size):
        results.extend(
            asyncio.run(provider.synthesize_batch(requests[offset : offset + batch_size]))
        )
    wall_seconds = time.monotonic() - started
    audio_seconds = sum(r.duration_ms / 1000.0 for r in results)
    rtf = audio_seconds / wall_seconds if wall_seconds else 0.0
    return [
        {
            "backend": getattr(provider, "backend", "fake"),
            "batch_size": batch_size,
            "items": len(requests),
            "wall_seconds": round(wall_seconds, 6),
            "audio_seconds": round(audio_seconds, 6),
            "rtf": round(rtf, 6),
            "realtime_x": round(rtf, 6),
            "items_per_second": round(len(requests) / wall_seconds, 3) if wall_seconds else 0.0,
        }
    ]


def _parse_batch_sizes(raw: str) -> tuple[int, ...]:
    return tuple(int(v) for v in raw.split(",") if v.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Direct TTS provider throughput benchmark (Change T 14.1-14.6). "
            "Default --mode fake for CI-safe smoke; --mode real needs the SDK "
            "+ model weights."
        )
    )
    parser.add_argument("--mode", choices=("fake", "real"), default="fake")
    parser.add_argument("--batch-sizes", default="1,4,8,16,32", help="comma list, e.g. 1,4,8,16,32")
    parser.add_argument("--samples", type=int, default=64, help="total items per cell")
    parser.add_argument("--corpus", choices=("fixed",), default="fixed", help="corpus source")
    parser.add_argument("--output", default="", help="write JSON to this path (else stdout)")
    parser.add_argument("--provider", default="vieneu_v3")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--accelerator", choices=("auto", "cpu", "gpu"), default="auto")
    parser.add_argument(
        "--no-sleep", action="store_true", help="fake mode: disable simulated inference delay"
    )
    parser.add_argument(
        "--reference",
        action="store_true",
        help="print the historical T4 reference evidence (not an SLA)",
    )
    args = parser.parse_args(argv)

    batch_sizes = _parse_batch_sizes(args.batch_sizes)
    if not batch_sizes:
        parser.error("--batch-sizes must contain at least one value")

    if args.mode == "fake":
        provider = AsyncFakeProvider(no_sleep=args.no_sleep)
        results = []
        for batch_size in batch_sizes:
            results.extend(
                _run_sweep(
                    provider,
                    batch_size=batch_size,
                    samples=args.samples,
                    corpus=CORPUS,
                    voice_profile_id="default",
                )
            )
        config_meta = {
            "mode": "fake",
            "provider": "fake",
            "model_revision": "fake-1",
            "accelerator": "none",
            "corpus": "fixed",
        }
    else:
        # Real provider: GPU sweep 1/4/8/16/32 on pytorch; CPU is batch-1 only
        # (14.5 — no CPU batch sweep until upstream capability changes).
        if args.accelerator == "cpu" and batch_sizes != (1,):
            print(
                "note: CPU mode measures single-path compatibility only; forcing batch-sizes=1",
                file=sys.stderr,
            )
            batch_sizes = (1,)
        real = _build_real_provider(args.accelerator, args.model_revision)
        provider = AsyncRealProvider(real)
        results = []
        for batch_size in batch_sizes:
            results.extend(
                _run_sweep(
                    provider,
                    batch_size=batch_size,
                    samples=args.samples,
                    corpus=CORPUS,
                    voice_profile_id="default",
                )
            )
        config_meta = {
            "mode": "real",
            "provider": args.provider,
            "model_revision": real.capabilities().model_revision,
            "accelerator": args.accelerator,
            "backend": real.backend,
            "corpus": "fixed",
        }

    payload = {
        "config": {**config_meta, "hardware": hardware_metadata()},
        "results": results,
    }
    if args.reference:
        payload["reference"] = _T4_REFERENCE_NOTE
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
