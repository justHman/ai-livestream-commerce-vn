"""Unit tests for TTSEngine.stream_audio() (Task 4) — AudioWindow streaming.

Covers the new streaming seam that consumes a TextChunk (or str) and yields
AudioWindow objects suitable for the avatar render stage:

  - ToneEngine.stream_audio on a long text -> multiple AudioWindows whose
    durations sum to the full chunk duration; seq 0..n-1; only last is_final.
  - TextChunk input propagates session_id/utterance_id into every AudioWindow.
  - Short text -> single window, is_final=True, seq=0.
  - Warmup no longer raises (the max_tokens bug is fixed).
  - TTSRequest has a temperature field (default 0.0) — guards the
    transformers adapter's `do_sample=req.temperature > 0` path.
  - Sample-rate preservation: a stub engine returning 16 kHz AudioChunks
    produces 16 kHz AudioWindows (NOT hardcoded 24 kHz).

All tests offline (ToneEngine + stub subclasses). No model downloads, no GPU.
"""

from __future__ import annotations

import numpy as np

from tts.engines.base import AudioWindow, TextChunk
from tts.engines.base import (
    AudioChunk,
    TTSEngine,
    TTSRequest,
    ToneEngine,
)


# ---------- helpers ----------


def _durations_ms(windows: list[AudioWindow]) -> list[int]:
    return [w.duration_ms for w in windows]


# ---------- stream_audio: multi-window on long text ----------


def test_stream_audio_yields_multiple_windows_for_long_text():
    """60-char text -> ToneEngine ~4 s -> multiple ~1 s windows."""
    engine = ToneEngine.from_config({})
    text = "a" * 60  # 60 chars / 15 chars-per-sec ~ 4 s at 24 kHz
    windows = list(engine.stream_audio(text))

    assert len(windows) >= 2, f"expected >=2 windows, got {len(windows)}"
    # seq increments from 0
    assert [w.seq for w in windows] == list(range(len(windows)))
    # only the last is final
    assert [w.is_final for w in windows] == [False] * (len(windows) - 1) + [True]
    # every window carries ToneEngine's native 24 kHz
    assert all(w.sample_rate == 24000 for w in windows)
    # durations sum to the full chunk duration (within 1 ms rounding)
    total_ms = sum(_durations_ms(windows))
    expected_ms = int(4 * 1000)  # ~4 s
    assert abs(total_ms - expected_ms) <= 1, (
        f"sum(duration_ms)={total_ms} != expected {expected_ms}"
    )
    # text_span stamped with the input text on every window
    assert all(w.text_span == text for w in windows)


# ---------- stream_audio: TextChunk input propagates metadata ----------


def test_stream_audio_propagates_textchunk_metadata():
    engine = ToneEngine.from_config({})
    chunk = TextChunk(
        session_id="sess-7",
        utterance_id="utt-9",
        seq=3,
        text="b" * 60,
        is_final=True,
    )
    windows = list(engine.stream_audio(chunk))

    assert len(windows) >= 2
    for w in windows:
        assert w.session_id == "sess-7"
        assert w.utterance_id == "utt-9"
        assert w.text_span == chunk.text
    assert windows[-1].is_final is True


def test_stream_audio_str_input_uses_explicit_session_utterance_ids():
    engine = ToneEngine.from_config({})
    windows = list(engine.stream_audio("c" * 60, session_id="s-str", utterance_id="u-str"))
    assert len(windows) >= 2
    for w in windows:
        assert w.session_id == "s-str"
        assert w.utterance_id == "u-str"


# ---------- stream_audio: short/single-window case ----------


def test_stream_audio_short_text_yields_single_final_window():
    """Short text -> ToneEngine's 0.6 s floor -> one window, is_final=True."""
    engine = ToneEngine.from_config({})
    windows = list(engine.stream_audio("hi"))
    assert len(windows) == 1
    assert windows[0].seq == 0
    assert windows[0].is_final is True
    assert windows[0].duration_ms > 0


# ---------- stream_audio: empty audio yields nothing ----------


def test_stream_audio_empty_pcm_yields_nothing():
    """A stub engine that returns zero-sample AudioChunk -> no windows."""
    engine = _StubEmptyEngine.from_config({})
    windows = list(engine.stream_audio("anything"))
    assert windows == []


# ---------- warmup: no longer swallows TypeError ----------


def test_tone_engine_warmup_completes_without_raising():
    """The max_tokens=16 bug used to TypeError internally and be swallowed.

    After the fix the call is valid, so warmup() returns None cleanly.
    """
    engine = ToneEngine.from_config({})
    # Should not raise.
    result = engine.warmup()
    assert result is None


def test_tone_engine_warmup_with_custom_text():
    engine = ToneEngine.from_config({})
    assert engine.warmup(text="Xin chào thế giới") is None


# ---------- TTSRequest.temperature field ----------


def test_tts_request_has_temperature_field_default_zero():
    """Guards the transformers adapter path `do_sample=req.temperature > 0`.

    With default 0.0 -> do_sample=False (deterministic), matching the
    pre-existing behaviour callers relied on.
    """
    req = TTSRequest(text="hi")
    assert hasattr(req, "temperature")
    assert req.temperature == 0.0


def test_tts_request_temperature_is_settable():
    req = TTSRequest(text="hi", temperature=0.7)
    assert req.temperature == 0.7


# ---------- sample-rate preservation ----------


def test_stream_audio_preserves_native_sample_rate_not_hardcoded():
    """A stub engine returning 16 kHz AudioChunks must produce 16 kHz windows.

    This guards against the bug where stream_audio hardcodes 24 kHz instead
    of using the AudioChunk's actual sample_rate.
    """

    engine = _Stub16kEngine.from_config({})
    windows = list(engine.stream_audio("d" * 60))
    assert len(windows) >= 1
    for w in windows:
        assert w.sample_rate == 16000, (
            f"expected 16 kHz, got {w.sample_rate} (hardcoded 24 kHz bug?)"
        )


# ---------- stub engines ----------


class _StubEmptyEngine(TTSEngine):
    """Returns an AudioChunk with zero samples -> stream_audio yields nothing."""

    name = "stub-empty"
    sample_rate = 24000

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubEmptyEngine":
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        return AudioChunk(pcm=np.zeros(0, dtype=np.float32), sample_rate=24000)


class _Stub16kEngine(TTSEngine):
    """Returns ~3 s of float32 audio at 16 kHz (NOT 24 kHz)."""

    name = "stub-16k"
    sample_rate = 16000

    @classmethod
    def from_config(cls, cfg: dict) -> "_Stub16kEngine":
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        # ~3 seconds at 16 kHz -> 48000 samples
        n = 16000 * 3
        t = np.arange(n) / 16000
        pcm = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        return AudioChunk(pcm=pcm, sample_rate=16000)
