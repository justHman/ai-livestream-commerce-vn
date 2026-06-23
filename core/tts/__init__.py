"""core.tts — model-agnostic TTS engine seam (swap model by config, not code).

Reality (researched 2026-06-22): no official framework wraps VieNeu + CosyVoice2
+ Kokoro + XTTS under one "drop-in weight" interface — each has its own loader,
codec, sample rate, and license. The production pattern is a THIN ABC + one
adapter per model (mirrors core/render's RenderBackend). Adding a model = one
adapter class + one config entry; the say-loop above never changes.

  TTSEngine.synthesize(req) -> AudioChunk        (blocking, full waveform)
  TTSEngine.stream(req)     -> Iterator[AudioChunk]  (override if model streams)

Engines register by name; `load_engine(cfg)` builds one from a config dict.
A built-in ToneEngine (no deps) keeps the loop testable offline.

Commercial-OK engines: VieNeu (Apache, VN-native, default), Kokoro (Apache),
CosyVoice2 (Apache). Research-only: XTTS (CPML), F5 (CC-BY-NC).
"""

from .base import (
    AudioChunk,
    TTSEngine,
    TTSRequest,
    ToneEngine,
    load_engine,
    register_engine,
    to_tts_fn,
)

__all__ = [
    "TTSEngine",
    "TTSRequest",
    "AudioChunk",
    "ToneEngine",
    "load_engine",
    "register_engine",
    "to_tts_fn",
]
