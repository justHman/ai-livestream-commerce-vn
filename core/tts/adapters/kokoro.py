"""Kokoro adapter — Apache-2.0, light (82M), CPU-friendly. (No native VN voice.)"""

from __future__ import annotations

import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


@register_engine("kokoro")
class KokoroAdapter(TTSEngine):
    sample_rate = 24_000

    def __init__(self) -> None:
        self._pipe = None
        self._voice = "vi_female"
        self._lang = "vi"

    @classmethod
    def from_config(cls, cfg: dict) -> "KokoroAdapter":
        from kokoro import KPipeline  # pip install kokoro

        e = cls()
        e._lang = cfg.get("lang", "vi")
        e._voice = cfg.get("voice", "vi_female")
        e._pipe = KPipeline(
            lang_code=e._lang,
            repo_id=cfg.get("weights_path", "hexgrad/Kokoro-82M"),
        )
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._pipe is None:
            raise RuntimeError("Kokoro not loaded")
        chunks = [np.asarray(a, dtype=np.float32)
                  for _, _, a in self._pipe(req.text, voice=req.voice or self._voice)]
        wav = np.concatenate(chunks) if chunks else np.zeros(1, np.float32)
        return AudioChunk(pcm=wav, sample_rate=self.sample_rate)
