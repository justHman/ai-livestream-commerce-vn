"""Provider-neutral synthesis request/result types (Change T tasks 2.3/2.4).

These are the only shapes the scheduler and API layers see. Audio is carried
as bytes or float32 waveform ndarray — never provider tensors, speaker
embeddings, or reference codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import numpy as np

from tts.providers.errors import ProviderError


class Priority(str, Enum):
    """Provider-neutral scheduling priority. Why a request is high is a
    future-spec concern; Change T only defines scheduler semantics."""

    NORMAL = "normal"
    HIGH = "high"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GenerationConfig:
    """Provider-neutral sampling knobs passed through to the provider."""

    speed: float = 1.0
    seed: int = 42
    temperature: float = 0.8


@dataclass(frozen=True)
class SynthesisRequest:
    """Immutable identity of one synthesis unit (one HTTP speech chunk)."""

    request_id: str
    session_id: str
    utterance_id: str
    chunk_seq: int
    input_text: str
    tenant_id: str = "default"
    voice_profile_id: str = "default"
    style: str = "natural"
    priority: Priority = Priority.NORMAL
    response_format: str = "wav"
    generation_config: GenerationConfig = field(default_factory=GenerationConfig)
    submitted_at: datetime = field(default_factory=_now_utc)
    deadline_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.chunk_seq < 0:
            raise ValueError("chunk_seq must be >= 0")
        if self.deadline_at is None:
            # Deadline defaults to submission time plus the service deadline
            # bound; the scheduler overrides from its own config at admission.
            object.__setattr__(
                self, "deadline_at", self.submitted_at + timedelta(milliseconds=30_000)
            )


@dataclass(frozen=True)
class AudioResult:
    """Canonical audio output: waveform (float32 mono in [-1, 1]) or bytes.

    Exactly one of ``waveform``/``audio_bytes`` is set. ``audio_bytes`` is the
    already-encoded response format (e.g. WAV container). Provider inference
    yields a waveform; the API layer encodes to the response format.
    """

    request_id: str
    sample_rate: int
    waveform: Optional[np.ndarray] = None
    audio_bytes: Optional[bytes] = None
    response_format: str = "wav"
    duration_ms: int = 0
    error: Optional[ProviderError] = None

    def __post_init__(self) -> None:
        if self.error is None:
            if self.waveform is None and self.audio_bytes is None:
                raise ValueError("AudioResult needs waveform or audio_bytes")
        else:
            if self.waveform is not None or self.audio_bytes is not None:
                raise ValueError("error result must not carry audio")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")


# Typed aliases for the provider boundary (task 2.4): the provider receives
# exactly what the scheduler admitted, and returns the request identity plus
# audio. Nothing provider-specific crosses this boundary.
ProviderRequest = SynthesisRequest
ProviderResult = AudioResult
