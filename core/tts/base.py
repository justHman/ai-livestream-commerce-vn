"""TTSEngine ABC + registry + adapters-friendly base.

The seam that makes TTS models swappable by config. Adapters (vieneu/kokoro/
cosyvoice/xtts) live in core/tts/adapters/ and import each model's OFFICIAL
code, exposing this one interface. The Director/RenderBackend never depends on
a specific model.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import numpy as np


@dataclass
class TTSRequest:
    """One synthesis request."""

    text: str
    voice: Optional[str] = None        # voice id OR ref-audio path (zero-shot clone)
    ref_text: Optional[str] = None     # transcript of ref audio (NeuTTS/F5 need it)
    language: str = "vi"
    speed: float = 1.0
    seed: int = 42


@dataclass
class AudioChunk:
    """A chunk of audio as float32 mono in [-1, 1] at `sample_rate`."""

    pcm: np.ndarray
    sample_rate: int

    def to_pcm16_bytes(self) -> bytes:
        clipped = np.clip(self.pcm, -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()


class TTSEngine(ABC):
    """Model-agnostic TTS interface. One adapter per model implements it."""

    name: str = "abstract"
    sample_rate: int = 24_000

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: dict) -> "TTSEngine":
        """Build from cfg = {engine, weights_path, device, dtype, ...}."""
        ...

    @abstractmethod
    def synthesize(self, req: TTSRequest) -> AudioChunk:
        """Blocking full-waveform synthesis."""
        ...

    def stream(self, req: TTSRequest) -> Iterator[AudioChunk]:
        """Default: single chunk. Override for true streaming models."""
        yield self.synthesize(req)

    def warmup(self, text: str = "Xin chào") -> None:
        """Pre-warm the engine: synthesize one dummy utterance to JIT CUDA
        kernels + allocate GPU buffers. Call once after from_config.

        Effect: first real synthesis skips CUDA kernel JIT (~200-500ms saved).
        Non-fatal if it fails.
        """
        try:
            self.synthesize(TTSRequest(text=text, max_tokens=16))
        except Exception:
            pass

    def unload(self) -> None:
        """Free VRAM when swapping models (override if needed)."""
        return None


# ── Registry ────────────────────────────────────────────────────────

ENGINES: dict[str, type[TTSEngine]] = {}


def register_engine(name: str):
    def deco(cls: type[TTSEngine]) -> type[TTSEngine]:
        ENGINES[name] = cls
        cls.name = name
        return cls
    return deco


def load_engine(cfg: dict) -> TTSEngine:
    """Build an engine from config. cfg['engine'] selects the adapter.

    Importing core.tts.adapters registers the real adapters lazily; if a model's
    deps are missing, that adapter raises on from_config (not at import).
    """
    engine = cfg.get("engine", "tone")
    if engine not in ENGINES:
        # lazy-import adapters so optional model deps don't break base import
        try:
            from . import adapters  # noqa: F401  (registers adapters)
        except Exception:
            pass
    if engine not in ENGINES:
        raise KeyError(f"unknown TTS engine '{engine}'. Registered: {sorted(ENGINES)}")
    return ENGINES[engine].from_config(cfg)


def to_tts_fn(engine: TTSEngine, voice: Optional[str] = None,
              ref_text: Optional[str] = None) -> Callable[[str], tuple[bytes, int]]:
    """Adapt a TTSEngine to the (pcm16_bytes, rate) callable the RenderBackend
    expects (core.render.cloud.configure(tts_fn=...))."""
    def tts_fn(text: str) -> tuple[bytes, int]:
        chunk = engine.synthesize(
            TTSRequest(text=text, voice=voice, ref_text=ref_text)
        )
        return chunk.to_pcm16_bytes(), chunk.sample_rate
    return tts_fn


# ── Built-in offline engine (no deps) ───────────────────────────────


@register_engine("tone")
class ToneEngine(TTSEngine):
    """Deterministic sine-tone engine for offline tests / fallback."""

    sample_rate = 24_000

    @classmethod
    def from_config(cls, cfg: dict) -> "ToneEngine":
        e = cls()
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        return e

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        seconds = max(0.6, min(len(req.text) / 15.0, 6.0))
        n = int(self.sample_rate * seconds)
        t = np.arange(n) / self.sample_rate
        pcm = (0.4 * np.sin(2 * math.pi * 330 * t)).astype(np.float32)
        return AudioChunk(pcm=pcm, sample_rate=self.sample_rate)
