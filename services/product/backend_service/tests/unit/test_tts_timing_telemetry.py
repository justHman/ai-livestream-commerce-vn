"""Bounded EWMA / TTS-timing telemetry tests (task 7.2)."""

from __future__ import annotations

import pytest

from backend.application.speech_chunking.telemetry import BoundedEwma, TelemetryCollector
from backend.application.speech_chunking.types import RuntimeHints


def test_bounded_ewma_deterministic() -> None:
    first = BoundedEwma(alpha=0.3, window=8)
    second = BoundedEwma(alpha=0.3, window=8)
    samples = [1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 89.0]
    for value in samples:
        first.update(value)
        second.update(value)
    assert first.value == pytest.approx(second.value)
    assert first.window_size == 8


def test_bounded_ewma_window_bounded() -> None:
    ewma = BoundedEwma(alpha=0.3, window=8)
    for value in range(20):
        ewma.update(float(value))
    assert len(ewma._values) <= 8  # private probe: bounded fixed window


def test_ewma_empty_returns_none() -> None:
    assert BoundedEwma(alpha=0.3).value is None


def test_ewma_ignores_non_finite() -> None:
    ewma = BoundedEwma(alpha=0.3)
    ewma.update(float("nan"))
    ewma.update(float("inf"))
    assert ewma.value is None
    ewma.update(10.0)
    ewma.update(float("nan"))
    assert ewma.value == pytest.approx(10.0)


def test_ewma_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        BoundedEwma(alpha=0.0)
    with pytest.raises(ValueError):
        BoundedEwma(alpha=1.5)
    with pytest.raises(ValueError):
        BoundedEwma(alpha=0.3, window=0)


def test_ewma_neutral_degradation() -> None:
    collector = TelemetryCollector()
    hints = collector.to_runtime_hints()
    assert hints == RuntimeHints()
    assert hints.tts_first_audio_ewma_ms is None
    assert hints.tts_rtf_ewma is None


def test_rtf_uses_synthesis_over_duration() -> None:
    collector = TelemetryCollector()
    collector.record_tts_timing(1000.0, 2000.0)
    hints = collector.to_runtime_hints()
    assert hints.tts_rtf_ewma == pytest.approx(0.5)
    assert hints.tts_first_audio_ewma_ms == pytest.approx(1000.0)


def test_rtf_skips_zero_duration() -> None:
    collector = TelemetryCollector()
    collector.record_tts_timing(1000.0, 0.0)
    hints = collector.to_runtime_hints()
    assert hints.tts_rtf_ewma is None
    assert hints.tts_first_audio_ewma_ms == pytest.approx(1000.0)


def test_to_runtime_hints_maps_ewmas() -> None:
    collector = TelemetryCollector(ewma_alpha=0.3, ewma_window=8)
    first_audio = BoundedEwma(alpha=0.3, window=8)
    rtf = BoundedEwma(alpha=0.3, window=8)
    samples = [(100.0, 400.0), (150.0, 300.0), (200.0, 500.0)]
    for synthesis, duration in samples:
        collector.record_tts_timing(synthesis, duration)
        first_audio.update(synthesis)
        rtf.update(synthesis / duration)
    hints = collector.to_runtime_hints()
    assert hints.tts_first_audio_ewma_ms == pytest.approx(first_audio.value)
    assert hints.tts_rtf_ewma == pytest.approx(rtf.value)
    assert hints.speech_start_elapsed_ms == 0.0
    assert hints.playback_buffer_ms is None


def test_record_tts_timing_missing_degrades_neutral() -> None:
    collector = TelemetryCollector()
    collector.record_tts_timing(500.0, 0.0)
    hints = collector.to_runtime_hints()
    assert hints.tts_first_audio_ewma_ms == pytest.approx(500.0)
    assert hints.tts_rtf_ewma is None


def test_record_tts_timing_ignores_non_finite() -> None:
    collector = TelemetryCollector()
    collector.record_tts_timing(float("nan"), 1000.0)  # non-finite synthesis: skip
    collector.record_tts_timing(100.0, float("nan"))  # non-finite duration: no RTF
    hints = collector.to_runtime_hints()
    assert hints.tts_first_audio_ewma_ms == pytest.approx(100.0)
    assert hints.tts_rtf_ewma is None
