"""Reference WAV validation before provider enrollment (Change T task 5.1).

Validates shape (decodeability, channels, sample rate, duration, byte size)
with stdlib ``wave`` only. The provider (cluster 4) does the actual encoding;
this module exists so malformed/oversized audio never reaches it.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

VALID_SAMPLE_RATES = frozenset({8_000, 16_000, 22_050, 24_000, 44_100, 48_000})


class InvalidReferenceAudioError(ValueError):
    """Reference audio failed one of the enrollment constraints."""


@dataclass(frozen=True)
class ReferenceAudioInfo:
    sample_rate: int
    duration_ms: int
    channels: int


def validate_reference_audio(
    data: bytes, *, max_bytes: int = 10 * 1024 * 1024, max_seconds: int = 30
) -> ReferenceAudioInfo:
    """Validate WAV reference audio; return its shape or raise.

    Constraints (in order): byte bound, RIFF/WAV decodability, mono or stereo,
    a supported sample rate, and a duration within ``max_seconds``.
    """
    if not data:
        raise InvalidReferenceAudioError("reference audio is empty")
    if len(data) > max_bytes:
        raise InvalidReferenceAudioError(
            f"reference audio exceeds {max_bytes} bytes ({len(data)} bytes)"
        )
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            duration_ms = int(wf.getnframes() / sample_rate * 1000)
    except (wave.Error, EOFError, ValueError) as exc:
        raise InvalidReferenceAudioError("reference audio is not a decodable WAV file") from exc
    if channels not in (1, 2):
        raise InvalidReferenceAudioError(
            f"reference audio must be mono or stereo, got {channels} channels"
        )
    if sample_rate not in VALID_SAMPLE_RATES:
        raise InvalidReferenceAudioError(
            f"reference audio sample rate {sample_rate} is not supported "
            f"(expected one of {sorted(VALID_SAMPLE_RATES)})"
        )
    if duration_ms > max_seconds * 1000:
        raise InvalidReferenceAudioError(
            f"reference audio is {duration_ms} ms, exceeding the {max_seconds} s limit"
        )
    return ReferenceAudioInfo(sample_rate=sample_rate, duration_ms=duration_ms, channels=channels)
