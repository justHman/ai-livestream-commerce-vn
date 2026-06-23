"""XTTS-v2 adapter — voice-clone, streaming. WEIGHTS ARE NON-COMMERCIAL (CPML).

Use for research/comparison only; do NOT ship commercially with XTTS-v2 weights.
"""

from __future__ import annotations

import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


@register_engine("xtts")
class XTTSAdapter(TTSEngine):
    sample_rate = 24_000

    def __init__(self) -> None:
        self._tts = None
        self._ref = None
        self._lang = "vi"

    @classmethod
    def from_config(cls, cfg: dict) -> "XTTSAdapter":
        from TTS.api import TTS  # pip install coqui-tts

        e = cls()
        e._tts = TTS(cfg.get("weights_path", "tts_models/multilingual/multi-dataset/xtts_v2"))
        if cfg.get("device") == "cuda":
            e._tts.to("cuda")
        e._ref = cfg.get("ref_audio")
        e._lang = cfg.get("lang", "vi")
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._tts is None:
            raise RuntimeError("XTTS not loaded")
        wav = self._tts.tts(
            text=req.text,
            speaker_wav=req.voice or self._ref,
            language=req.language or self._lang,
        )
        return AudioChunk(pcm=np.asarray(wav, dtype=np.float32).reshape(-1),
                          sample_rate=self.sample_rate)
