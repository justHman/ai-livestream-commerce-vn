"""VieNeu-TTS adapter (DEFAULT) — Apache-2.0, Vietnamese-native.

VieNeu-TTS (pnnbao-ump/VieNeu-TTS-v2 | -v3-Turbo) is a NeuTTS-style model
(Qwen LM backbone + neural audio codec). It runs via its OFFICIAL runtime
(the neuttsair / vieneu package), NOT a transformers TTS pipeline.

This adapter defers the heavy import to from_config() and normalizes output to
float32 mono. The exact call surface must be confirmed against the model card
on a GPU run (see VERIFY note) — kept in ONE place so swapping the call is a
one-file change.
"""

from __future__ import annotations

import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


@register_engine("vieneu")
class VieNeuAdapter(TTSEngine):
    sample_rate = 24_000

    def __init__(self) -> None:
        self._model = None
        self._default_ref = None

    @classmethod
    def from_config(cls, cfg: dict) -> "VieNeuAdapter":
        e = cls()
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        e._default_ref = cfg.get("ref_audio")
        model_id = cfg.get("weights_path", "pnnbao-ump/VieNeu-TTS-v2")
        device = cfg.get("device", "cuda")

        # VERIFY on GPU: confirm the official package + class name from the
        # VieNeu/NeuTTS model card. Two known import paths; try both.
        try:
            from neuttsair.neutts import NeuTTSAir  # neuphonic/neutts-air runtime
            e._model = NeuTTSAir(model_id=model_id, device=device)
            e._impl = "neuttsair"
        except Exception:
            from vieneu import VieNeuTTS  # project package, if published
            e._model = VieNeuTTS(model_id=model_id, device=device)
            e._impl = "vieneu"
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        if self._model is None:
            raise RuntimeError("VieNeu not loaded; call from_config on a GPU box")
        ref = req.voice or self._default_ref
        # Both runtimes return a float32 waveform at self.sample_rate.
        if self._impl == "neuttsair":
            wav = self._model.infer(req.text, ref_audio=ref, ref_text=req.ref_text)
        else:
            wav = self._model.synthesize(req.text, ref_audio=ref)
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        return AudioChunk(pcm=wav, sample_rate=self.sample_rate)
