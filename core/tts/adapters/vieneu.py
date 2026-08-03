"""VieNeu-TTS adapter (NATIVE FALLBACK) — Apache-2.0, Vietnamese-native.

VieNeu-TTS (pip install vieneu; github.com/pnnbao97/VieNeu-TTS) is a
NeuTTS-style model (Qwen LM backbone + neural audio codec). It is NOT on
HuggingFace AutoModelForTextToWaveform, so it needs its own runtime package.

Kept as a native fallback because:
  - It is Vietnamese-native (trained on VN speech) → best for RQ3
    (VN sales-persuasive prosody vs generic MMS-VITS).
  - The transformers primary adapter covers MMS-VN/Bark/SpeechT5 but not NeuTTS.

Phase A notes:
  - The public API is ``from vieneu import Vieneu``; ``Vieneu(...)`` is a
    factory returning a variant-specific model (``V3TurboVieNeuTTS`` for
    ``mode="v3turbo"``, etc). There is no ``neuttsair``/``NeuTTSAir`` class in
    this package — that name belongs to the unrelated English upstream
    ``neutts`` package and must not be imported here.
  - v3-Turbo emits 48 kHz audio; v2 emits 24 kHz. We detect the variant from
    the weights path (case-insensitive substring ``v3-turbo``) and map it to
    the factory's ``mode`` kwarg.
  - The current ``vieneu`` release has no streaming inference API (no
    ``infer_stream``); ``stream()`` always yields a single full-waveform
    chunk from :meth:`synthesize`.
  - ``backend="auto"`` (the factory default) picks ONNX on CPU / PyTorch on
    GPU. ONNX-CPU is the maintainer-recommended path for v3-Turbo — a GPU
    ONNX execution provider was tried and reverted upstream because the
    autoregressive KV-cache loop made GPU slower than CPU.
  - Load-time failures NO LONGER silently downgrade to a 440 Hz tone. The
    fallback is gated behind ``APP_ENV=dev`` AND ``ALLOW_TTS_FALLBACK=1`` so
    production failures surface as the engine_manager ``tts_load_error`` they
    actually are.

Usage:
    engine = load_engine({
        "engine": "vieneu",
        "model": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
        "device": "auto",
        "ref_audio": "path/to/voice_ref.wav",
    })
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


def _infer_sample_rate(weights_path: str, override: Optional[int]) -> int:
    """Pick the native sample rate based on the VieNeu variant.

    v3-Turbo → 48 kHz (per model card).  v2 (and unknown) → 24 kHz.  An
    explicit ``sample_rate`` cfg value always wins so callers can override.
    """
    if override:
        return int(override)
    if weights_path and "v3-turbo" in weights_path.lower():
        return 48_000
    return 24_000


def _fallback_allowed() -> bool:
    """Tone fallback is only allowed in dev + when explicitly opted in."""
    return (
        os.environ.get("APP_ENV", "").lower() == "dev"
        and os.environ.get("ALLOW_TTS_FALLBACK", "") == "1"
    )


@register_engine("vieneu")
class VieNeuAdapter(TTSEngine):
    sample_rate = 24_000

    def __init__(self) -> None:
        self._model = None
        self._impl = ""  # "vieneu" | "tone-fallback"
        self._default_ref = None

    @classmethod
    def from_config(cls, cfg: dict) -> "VieNeuAdapter":
        e = cls()
        model_id = cfg.get("model") or cfg.get("weights_path", "pnnbao-ump/VieNeu-TTS-v2")
        e.sample_rate = _infer_sample_rate(model_id, cfg.get("sample_rate"))
        e._default_ref = cfg.get("ref_audio")
        device = cfg.get("device", "auto")
        mode = "v3turbo" if "v3-turbo" in model_id.lower() else "v2"

        try:
            from vieneu import Vieneu  # pip install vieneu (github.com/pnnbao97/VieNeu-TTS)

            e._model = Vieneu(mode=mode, backbone_repo=model_id, device=device)
            e._impl = "vieneu"
        except Exception:
            # NO silent tone fallback. Re-raise so engine_manager records the
            # failure in tts_load_error and /health/ready reports not-ready
            # honestly. Only honour the legacy tone fallback in opt-in dev mode.
            if not _fallback_allowed():
                raise
            # Dev opt-in: leave model unset so synthesize() emits a tone.
            e._model = None
            e._impl = "tone-fallback"
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._model is None:
            if self._impl == "tone-fallback":
                # Opt-in dev fallback: 440 Hz so the pipeline keeps running
                # locally even if vieneu is not installed.
                import math

                seconds = max(0.6, min(len(req.text) / 15.0, 6.0))
                n = int(self.sample_rate * seconds)
                t = np.arange(n) / self.sample_rate
                pcm = (0.4 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
                return AudioChunk(pcm=pcm, sample_rate=self.sample_rate)
            raise RuntimeError("VieNeu not loaded; check tts_load_error / pip install vieneu")
        ref = req.voice or self._default_ref
        if ref and req.ref_text:
            wav = self._model.infer(req.text, ref_audio=ref, ref_text=req.ref_text)
        elif ref:
            wav = self._model.infer(req.text, ref_audio=ref)
        else:
            wav = self._model.infer(req.text)
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        return AudioChunk(pcm=wav, sample_rate=self.sample_rate)

    def stream(self, req: TTSRequest) -> Iterator[AudioChunk]:
        """The current ``vieneu`` release has no streaming inference API.

        Always yields a single full-waveform chunk from :meth:`synthesize`.
        """
        yield self.synthesize(req)

    def unload(self) -> None:
        self._model = None
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
