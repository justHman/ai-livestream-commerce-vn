"""CosyVoice2 adapter — Apache-2.0, streaming, multilingual (vi not official)."""

from __future__ import annotations

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
        e._model = CosyVoice2(cfg.get("weights_path", "FunAudioLLM/CosyVoice2-0.5B"))
        e._ref = cfg.get("ref_audio")
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._model is None:
            raise RuntimeError("CosyVoice2 not loaded")
        # zero-shot: needs a ref wav + its transcript. Collect generator output.
        out = []
        for seg in self._model.inference_zero_shot(
            req.text, req.ref_text or "", req.voice or self._ref, stream=False
        ):
            out.append(np.asarray(seg["tts_speech"], dtype=np.float32).reshape(-1))
        wav = np.concatenate(out) if out else np.zeros(1, np.float32)
        return AudioChunk(pcm=wav, sample_rate=self.sample_rate)
