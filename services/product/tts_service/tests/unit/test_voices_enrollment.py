"""Reference WAV validation for enrollment (5.1)."""

from __future__ import annotations

import io
import wave

import pytest

from tts.voices.enrollment import (
    InvalidReferenceAudioError,
    VALID_SAMPLE_RATES,
    validate_reference_audio,
)


def make_wav(
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    duration_s: float = 1.0,
    frames: int | None = None,
) -> bytes:
    n_frames = frames if frames is not None else int(sample_rate * duration_s)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return buffer.getvalue()


def test_valid_mono_wav_accepted() -> None:
    info = validate_reference_audio(make_wav(sample_rate=16_000, duration_s=2.0))
    assert info.sample_rate == 16_000
    assert info.channels == 1
    assert 1900 <= info.duration_ms <= 2100


def test_stereo_wav_accepted() -> None:
    info = validate_reference_audio(make_wav(sample_rate=44_100, channels=2, duration_s=1.0))
    assert info.channels == 2


def test_empty_bytes_rejected() -> None:
    with pytest.raises(InvalidReferenceAudioError, match="empty"):
        validate_reference_audio(b"")


def test_oversized_bytes_rejected() -> None:
    data = make_wav(sample_rate=16_000, duration_s=5.0)
    with pytest.raises(InvalidReferenceAudioError, match="exceeds"):
        validate_reference_audio(data, max_bytes=1_000)


def test_garbage_rejected() -> None:
    with pytest.raises(InvalidReferenceAudioError, match="not a decodable WAV"):
        validate_reference_audio(b"RIFF....WAVEfmt garbage")


def test_overlong_audio_rejected() -> None:
    data = make_wav(sample_rate=8_000, duration_s=60.0)
    with pytest.raises(InvalidReferenceAudioError, match="exceeding"):
        validate_reference_audio(data, max_seconds=30)


def test_unsupported_sample_rate_rejected() -> None:
    data = make_wav(sample_rate=11_025)
    assert 11_025 not in VALID_SAMPLE_RATES
    with pytest.raises(InvalidReferenceAudioError, match="sample rate"):
        validate_reference_audio(data)


def test_truncated_wav_rejected() -> None:
    with pytest.raises(InvalidReferenceAudioError):
        validate_reference_audio(make_wav()[:40])
