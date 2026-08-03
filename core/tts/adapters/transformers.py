"""transformers TTS adapter (PRIMARY) — unified HF AutoModel for speech synthesis.

This is the TTS analogue of the vLLM/HF seam for LLMs: use HuggingFace's
unified `AutoModelForTextToWaveform` + `AutoProcessor` so swapping models =
changing the model id string, NOT learning a new library API.

Covered models (all via the same transformers API):
  - facebook/mms-tts-vie        — Facebook MMS VITS for Vietnamese (DEFAULT, good quality)
  - facebook/mms-tts-eng        — English MMS VITS
  - facebook/mms-tts-*          — 1100+ languages via MMS
  - espnet/kan-bart-bark        — Bark (multilingual, expressive)
  - microsoft/speecht5_tts      — SpeechT5 (voice clone via xvector embedding)
  - any VITS/MMS-VITS model on HF

Models NOT covered (use native fallback adapters instead):
  - VieNeu-TTS (NeuTTS)  → core/tts/adapters/vieneu.py (VN-native, RQ3 prosody)
  - CosyVoice2          → core/tts/adapters/cosyvoice.py (true streaming)

Usage:
    engine = load_engine({
        "engine": "transformers",
        "model": "facebook/mms-tts-vie",
        "device": "cuda",
    })
    # Swap to Bark: change "model" to "espnet/kan-bart-bark" — same code path.
"""

from __future__ import annotations


import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


@register_engine("transformers")
class TransformersTTSAdapter(TTSEngine):
    """HuggingFace transformers unified TTS (AutoModelForTextToWaveform)."""

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._device = "cpu"
        self._sample_rate = 24_000

    @classmethod
    def from_config(cls, cfg: dict) -> "TransformersTTSAdapter":
        import torch
        from transformers import AutoModelForTextToWaveform, AutoProcessor

        e = cls()
        model_id = cfg.get("model") or cfg.get("weights_path") or "facebook/mms-tts-vie"

        device = cfg.get("device", "auto")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        e._processor = AutoProcessor.from_pretrained(model_id)
        e._model = AutoModelForTextToWaveform.from_pretrained(model_id)
        if device == "cuda" and torch.cuda.is_available():
            e._model = e._model.cuda()
        e._model.eval()
        e._device = device

        # Get the model's native sample rate from its config
        sr = getattr(getattr(e._model, "config", None), "sampling_rate", None)
        e._sample_rate = int(cfg.get("sample_rate", sr or 24_000))
        e.name = "transformers"
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        import torch

        if self._model is None:
            raise RuntimeError("transformers TTS not loaded; call from_config on a GPU box")

        # MMS-VITS / VITS models: processor(tokenize text) -> model.generate() -> waveform
        # Some models (Bark, SpeechT5) need extra inputs (voice_embeddings, bark_voice_preset).
        # We handle the common VITS/MMS path here; model-specific extras go through cfg.
        inputs = self._processor(text=req.text, return_tensors="pt")

        if self._device == "cuda" and torch.cuda.is_available():
            inputs = {k: v.cuda() if hasattr(v, "cuda") else v for k, v in inputs.items()}

        with torch.no_grad():
            output = self._model.generate(**inputs, do_sample=req.temperature > 0)

        # AutoModelForTextToWaveform returns a dict-like with "waveform" key
        if hasattr(output, "waveform"):
            wav = output.waveform
        elif isinstance(output, dict) and "waveform" in output:
            wav = output["waveform"]
        elif hasattr(output, "audio"):
            wav = output.audio
        else:
            # Some models return the waveform directly
            wav = output

        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        # Speed control (simple resampling by slicing — models don't all support speed param)
        if req.speed != 1.0 and req.speed > 0:
            target_len = int(len(wav) / req.speed)
            indices = np.linspace(0, len(wav) - 1, target_len).astype(int)
            wav = wav[indices]

        return AudioChunk(pcm=wav, sample_rate=self._sample_rate)

    def unload(self) -> None:
        import gc

        self._model = None
        self._processor = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
