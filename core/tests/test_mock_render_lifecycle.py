"""Unit tests for MockRenderBackend lifecycle (Task 5).

Verifies start/stream/stop lifecycle, unknown-session KeyError handling,
StartResult shape, and basic stream_audio behavior (single VideoWindow
per AudioWindow, propagated fields). Frame content/animation is covered
by test_mock_frame_generation.py.

All tests offline (Pillow + numpy only). No network, no model downloads.
"""

from __future__ import annotations

import pytest

from core.render.base import StartOptions
from core.render.mock import MockRenderBackend
from core.render.windows import AudioWindow, VideoWindow, num_frames_for


# ---------- helpers ----------

SAMPLE_RATE = 24000  # 24 kHz -> 48 bytes/ms (int16 mono)


def _pcm_bytes(ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Deterministic int16 mono PCM bytes of the given duration."""
    samples = int(sample_rate * ms / 1000)
    n_bytes = samples * 2
    motif = b"\x01\x00\x02\x00"
    return (motif * (n_bytes // len(motif) + 1))[:n_bytes]


def _audio_window(ms: int = 300, is_final: bool = True) -> AudioWindow:
    return AudioWindow(
        session_id="test-session",
        utterance_id="utt-1",
        seq=0,
        sample_rate=SAMPLE_RATE,
        duration_ms=ms,
        pcm=_pcm_bytes(ms),
        is_final=is_final,
        text_span="xin chao",
    )


# ---------- start() ----------


def test_start_returns_start_result_with_mode_mock():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())
    assert result.mode == "MOCK"
    assert result.session_id, "session_id must be non-empty"
    assert result.session_id.startswith("mock-"), (
        f"session_id should start with 'mock-', got {result.session_id!r}"
    )
    assert result.livekit_url, "livekit_url must be non-empty"
    assert result.livekit_client_token, "livekit_client_token must be non-empty"


def test_start_public_dict_has_exactly_four_fields():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())
    d = result.public_dict()
    assert set(d.keys()) == {
        "session_id",
        "livekit_url",
        "livekit_client_token",
        "mode",
    }


# ---------- unknown session errors ----------


def test_stream_audio_unknown_session_raises_keyerror():
    backend = MockRenderBackend()
    with pytest.raises(KeyError):
        list(backend.stream_audio("nope", _audio_window()))


def test_interrupt_unknown_session_raises_keyerror():
    backend = MockRenderBackend()
    with pytest.raises(KeyError):
        backend.interrupt("nope")


def test_stop_unknown_session_raises_keyerror():
    backend = MockRenderBackend()
    with pytest.raises(KeyError):
        backend.stop("nope")


def test_session_status_unknown_session_raises_keyerror():
    backend = MockRenderBackend()
    with pytest.raises(KeyError):
        backend.session_status("nope")


def test_stop_then_session_status_raises_keyerror():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())
    sid = result.session_id
    backend.stop(sid)
    with pytest.raises(KeyError):
        backend.session_status(sid)


def test_stop_all_stops_each_active_session():
    backend = MockRenderBackend()
    first = backend.start(StartOptions())
    second = backend.start(StartOptions())

    backend.stop_all()

    assert first.session_id not in backend._sessions
    assert second.session_id not in backend._sessions


# ---------- session_status on started session ----------


def test_session_status_on_started_session_returns_string():
    backend = MockRenderBackend()
    result = backend.start(StartOptions())
    sid = result.session_id
    status = backend.session_status(sid)
    assert isinstance(status, str)
    assert status, "status must be non-empty"


# ---------- stream_audio on known session ----------


def test_stream_audio_yields_exactly_one_videowindow():
    backend = MockRenderBackend()
    sid = backend.start(StartOptions()).session_id
    aw = _audio_window(ms=300, is_final=True)
    windows = list(backend.stream_audio(sid, aw))
    assert len(windows) == 1, f"expected 1 VideoWindow, got {len(windows)}"


def test_stream_audio_propagates_fields():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id
    aw = _audio_window(ms=300, is_final=True)
    [vw] = list(backend.stream_audio(sid, aw))

    assert isinstance(vw, VideoWindow)
    assert vw.session_id == sid
    assert vw.utterance_id == aw.utterance_id
    assert vw.audio_window_id == aw.id
    assert vw.duration_ms == aw.duration_ms
    assert vw.fps == 25
    assert vw.is_final is True


def test_stream_audio_frame_count_matches_num_frames_for():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id
    aw = _audio_window(ms=300, is_final=True)
    [vw] = list(backend.stream_audio(sid, aw))
    expected = num_frames_for(aw, 25)
    assert len(vw.frames) == expected, (
        f"frames={len(vw.frames)} != num_frames_for={expected}"
    )
