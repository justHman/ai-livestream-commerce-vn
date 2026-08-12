"""Provider model/request/result behavior (Change T tasks 2.3/2.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tts.providers.errors import ProviderInferenceError
from tts.providers.models import (
    AudioResult,
    GenerationConfig,
    Priority,
    SynthesisRequest,
)


def _request(**overrides) -> SynthesisRequest:
    base = dict(
        request_id="req-1",
        session_id="sess-1",
        utterance_id="utt-1",
        chunk_seq=0,
        input_text="Xin chào",
    )
    base.update(overrides)
    return SynthesisRequest(**base)


def test_request_defaults() -> None:
    req = _request()
    assert req.priority == Priority.NORMAL
    assert req.voice_profile_id == "default"
    assert req.style == "natural"
    assert req.response_format == "wav"
    assert req.generation_config.speed == 1.0


def test_request_immutable() -> None:
    req = _request()
    with pytest.raises(AttributeError):
        req.input_text = "other"  # type: ignore[misc]


def test_deadline_defaults_to_submission_plus_bound() -> None:
    req = _request(submitted_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    assert req.deadline_at == req.submitted_at + timedelta(milliseconds=30_000)


def test_negative_chunk_seq_rejected() -> None:
    with pytest.raises(ValueError, match="chunk_seq"):
        _request(chunk_seq=-1)


def test_generation_config_carries_sampling_knobs() -> None:
    gc = GenerationConfig(speed=1.2, seed=7, temperature=0.5)
    assert (gc.speed, gc.seed, gc.temperature) == (1.2, 7, 0.5)


def test_audio_result_accepts_waveform() -> None:
    wf = np.zeros(48_000, dtype=np.float32)
    result = AudioResult(request_id="req-1", sample_rate=48_000, waveform=wf)
    assert result.sample_rate == 48_000


def test_audio_result_accepts_encoded_bytes() -> None:
    result = AudioResult(
        request_id="req-1", sample_rate=48_000, audio_bytes=b"RIFF", response_format="wav"
    )
    assert result.audio_bytes == b"RIFF"


def test_audio_result_requires_payload() -> None:
    with pytest.raises(ValueError, match="waveform or audio_bytes"):
        AudioResult(request_id="req-1", sample_rate=48_000)


def test_error_result_must_not_carry_audio() -> None:
    with pytest.raises(ValueError, match="must not carry audio"):
        AudioResult(
            request_id="req-1",
            sample_rate=48_000,
            waveform=np.zeros(10, dtype=np.float32),
            error=ProviderInferenceError("boom"),
        )


def test_audio_result_rejects_bad_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        AudioResult(request_id="req-1", sample_rate=0, audio_bytes=b"RIFF")
