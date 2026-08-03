"""TTSEngine ABC + registry + adapters-friendly base.

The seam that makes TTS models swappable by config. Adapters (vieneu/kokoro/
cosyvoice/xtts) live in core/tts/adapters/ and import each model's OFFICIAL
code, exposing this one interface. The Director/RenderBackend never depends on
a specific model.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Union
from uuid import uuid4

import numpy as np

try:
    from core.render.windows import AudioWindow, TextChunk, split_waveform
except ModuleNotFoundError:

    @dataclass(frozen=True)
    class TextChunk:
        """Service-local streamed text chunk."""

        session_id: str
        utterance_id: str
        seq: int
        text: str
        is_final: bool
        id: str = field(default_factory=lambda: uuid4().hex)

    @dataclass(frozen=True)
    class AudioWindow:
        """Service-local PCM audio window."""

        session_id: str
        utterance_id: str
        seq: int
        sample_rate: int
        duration_ms: int
        is_final: bool
        pcm: Optional[bytes] = None
        audio_path: Optional[str] = None
        text_span: Optional[str] = None
        id: str = field(default_factory=lambda: uuid4().hex)

    def split_waveform(
        pcm: bytes,
        sample_rate: int,
        min_ms: int,
        target_ms: int,
        max_ms: int,
        *,
        session_id: str = "",
        utterance_id: str = "",
    ) -> list[AudioWindow]:
        """Split int16 mono PCM into target-sized windows."""
        if sample_rate <= 0 or not (min_ms <= target_ms <= max_ms):
            raise ValueError("invalid audio window configuration")
        if not pcm:
            return []
        bytes_per_window = max(2, sample_rate * target_ms // 1000 * 2)
        chunks = [
            pcm[offset : offset + bytes_per_window]
            for offset in range(0, len(pcm), bytes_per_window)
        ]
        return [
            AudioWindow(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=index,
                sample_rate=sample_rate,
                duration_ms=len(chunk) // 2 * 1000 // sample_rate,
                pcm=chunk,
                is_final=index == len(chunks) - 1,
            )
            for index, chunk in enumerate(chunks)
        ]


class EngineError(RuntimeError):
    """Typed engine failure surfaced at the API boundary."""


class EngineUnavailable(EngineError):
    """Raised when no engine is started or the engine is not ready."""


@dataclass
class TTSRequest:
    """One synthesis request."""

    text: str
    voice: Optional[str] = None  # voice id OR ref-audio path (zero-shot clone)
    ref_text: Optional[str] = None  # transcript of ref audio (NeuTTS/F5 need it)
    language: str = "vi"
    speed: float = 1.0
    seed: int = 42
    # Sampling temperature. 0.0 = deterministic (greedy), matches the
    # `do_sample=req.temperature > 0` path in the transformers adapter without
    # requiring every caller to set it.
    temperature: float = 0.0


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

    def stream_audio(
        self,
        text_or_chunk: Union[TextChunk, str],
        *,
        session_id: str = "",
        utterance_id: str = "",
        req: Optional[TTSRequest] = None,
        min_ms: int = 500,
        target_ms: int = 1000,
        max_ms: int = 2000,
    ) -> Iterator[AudioWindow]:
        """Stream AudioWindows for one chunk of text.

        Accepts either a :class:`TextChunk` (uses its text/session_id/
        utterance_id) or a plain ``str`` (uses ``session_id``/``utterance_id``
        kwargs). Builds a :class:`TTSRequest` from the text (unless ``req`` is
        provided), calls :meth:`synthesize` once to get the full waveform,
        converts it to int16 PCM bytes, and splits it into AudioWindows via
        :func:`core.render.windows.split_waveform` using the window-size
        defaults from the avatar render plan (500/1000/2000 ms).

        The AudioWindow.sample_rate is the **native** sample rate reported by
        the adapter's AudioChunk — never hardcoded. The final window is marked
        ``is_final=True``; all others ``is_final=False``. ``seq`` increments
        from 0. ``text_span`` is set to the input text on every window.

        Adapters with a true streaming ``stream()`` (e.g. cosyvoice) MAY
        override this to convert incremental AudioChunks into AudioWindows
        directly, but the default non-streaming path is correct for any
        adapter that implements ``synthesize``.

        Yields nothing when ``synthesize`` returns an empty AudioChunk.
        """
        if isinstance(text_or_chunk, TextChunk):
            text = text_or_chunk.text
            sid = text_or_chunk.session_id or session_id
            uid = text_or_chunk.utterance_id or utterance_id
        else:
            text = text_or_chunk
            sid = session_id
            uid = utterance_id

        if req is None:
            req = TTSRequest(text=text)
        elif req.text is None:
            # Allow callers to pass a partially-built request.
            req = TTSRequest(
                text=text,
                voice=req.voice,
                ref_text=req.ref_text,
                language=req.language,
                speed=req.speed,
                seed=req.seed,
                temperature=req.temperature,
            )

        audio_chunk = self.synthesize(req)
        pcm_bytes = audio_chunk.to_pcm16_bytes()

        windows = split_waveform(
            pcm_bytes,
            audio_chunk.sample_rate,
            min_ms,
            target_ms,
            max_ms,
            session_id=sid,
            utterance_id=uid,
        )

        n = len(windows)
        for i, w in enumerate(windows):
            yield AudioWindow(
                session_id=w.session_id,
                utterance_id=w.utterance_id,
                seq=w.seq,
                sample_rate=w.sample_rate,
                duration_ms=w.duration_ms,
                pcm=w.pcm,
                audio_path=w.audio_path,
                text_span=text,
                is_final=(i == n - 1),
                id=w.id,
            )

    def warmup(self, text: str = "Xin chào") -> None:
        """Pre-warm the engine: synthesize one dummy utterance to JIT CUDA
        kernels + allocate GPU buffers. Call once after from_config.

        Effect: first real synthesis skips CUDA kernel JIT (~200-500ms saved).
        Non-fatal if it fails.
        """
        try:
            self.synthesize(TTSRequest(text=text))
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
    if engine in ("none", "", None):
        return ENGINES["tone"].from_config(cfg)
    if engine not in ENGINES:
        # lazy-import adapters so optional model deps don't break base import
        try:
            from . import cosyvoice, vieneu  # noqa: F401
        except Exception:
            pass
    if engine not in ENGINES:
        raise KeyError(f"unknown TTS engine '{engine}'. Registered: {sorted(ENGINES)}")
    return ENGINES[engine].from_config(cfg)


def to_tts_fn(
    engine: TTSEngine, voice: Optional[str] = None, ref_text: Optional[str] = None
) -> Callable[[str], tuple[bytes, int]]:
    """Adapt a TTSEngine to the (pcm16_bytes, rate) callable the RenderBackend
    expects (core.render.cloud.configure(tts_fn=...))."""

    def tts_fn(text: str) -> tuple[bytes, int]:
        chunk = engine.synthesize(TTSRequest(text=text, voice=voice, ref_text=ref_text))
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
