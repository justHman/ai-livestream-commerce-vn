"""CosyVoice2 adapter (NATIVE FALLBACK) — Apache-2.0, true streaming.

CosyVoice2 (FunAudioLLM/CosyVoice2-0.5B) is a streaming multilingual TTS.
It is NOT on HuggingFace AutoModelForTextToWaveform, so it needs its own
runtime (the cosyvoice repo). Kept as a native fallback for when true
sub-200ms streaming is needed (the transformers primary adapter does
full-waveform synthesis, not streaming).

Use cases:
  - True streaming TTS (token-by-token output for lowest latency).
  - Zero-shot voice cloning with a reference audio + transcript.
  - Multilingual (vi not officially supported but works with VN finetune).

Usage:
    engine = load_engine({
        "engine": "cosyvoice",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "ref_audio": "path/to/voice_ref.wav",
    })
"""

from __future__ import annotations

from typing import Iterator, Optional

import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


@register_engine("cosyvoice")
class CosyVoice2Adapter(TTSEngine):
    sample_rate = 24_000

    def __init__(self) -> None:
        self._model = None
        self._ref = None

    @classmethod
    def from_config(cls, cfg: dict) -> "CosyVoice2Adapter":
        from cosyvoice.cli.cosyvoice import CosyVoice2  # FunAudioLLM/CosyVoice repo

        e = cls()
        model_id = cfg.get("model") or cfg.get("weights_path", "FunAudioLLM/CosyVoice2-0.5B")
        e._model = CosyVoice2(model_id)
        e._ref = cfg.get("ref_audio")
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._model is None:
            raise RuntimeError("CosyVoice2 not loaded")
        out = []
        for seg in self._model.inference_zero_shot(
            req.text, req.ref_text or "", req.voice or self._ref, stream=False
        ):
            out.append(np.asarray(seg["tts_speech"], dtype=np.float32).reshape(-1))
        wav = np.concatenate(out) if out else np.zeros(1, np.float32)
        return AudioChunk(pcm=wav, sample_rate=self.sample_rate)

    def stream(self, req: TTSRequest) -> Iterator[AudioChunk]:
        """True streaming: yield AudioChunks as they are generated.

        Falls back to a single full-waveform chunk via :meth:`synthesize`
        when the streaming call raises or yields nothing.
        """
        if self._model is None:
            raise RuntimeError("CosyVoice2 not loaded")
        try:
            emitted = False
            for seg in self._model.inference_zero_shot(
                req.text, req.ref_text or "", req.voice or self._ref, stream=True
            ):
                pcm = np.asarray(seg["tts_speech"], dtype=np.float32).reshape(-1)
                if pcm.size == 0:
                    continue
                emitted = True
                yield AudioChunk(pcm=pcm, sample_rate=self.sample_rate)
            if not emitted:
                yield self.synthesize(req)
        except Exception:
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
