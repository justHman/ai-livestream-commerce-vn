"""Unit tests for MockRenderBackend frame generation (Task 5).

Verifies that stream_audio produces real decodable JPEG frames at the
configured dimensions, that frames differ over time (animation), and
that get_last_frame_png returns a decodable PNG.

All tests offline (Pillow + numpy only).
"""

from __future__ import annotations

import io

from PIL import Image

from core.render.base import StartOptions
from core.render.mock import MockRenderBackend
from core.render.windows import AudioWindow, num_frames_for


# ---------- helpers ----------

SAMPLE_RATE = 24000  # 24 kHz -> 48 bytes/ms (int16 mono)


def _pcm_bytes(ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Deterministic int16 mono PCM bytes of the given duration.

    A varying pattern (sinusoid-like via repeated motif) so RMS-derived
    mouth openness changes across the window.
    """
    import numpy as np

    n = int(sample_rate * ms / 1000)
    t = np.arange(n) / sample_rate
    wave = (np.sin(2 * 3.14159 * 220 * t) * 16000).astype("<i2")
    return wave.tobytes()


def _audio_window(ms: int = 1000, is_final: bool = True) -> AudioWindow:
    return AudioWindow(
        session_id="test-session",
        utterance_id="utt-1",
        seq=0,
        sample_rate=SAMPLE_RATE,
        duration_ms=ms,
        pcm=_pcm_bytes(ms),
        is_final=is_final,
        text_span="mot cua so ban dau",
    )


# ---------- frames are real JPEGs at configured dims ----------


def test_frames_decode_to_configured_dimensions():
    backend = MockRenderBackend(fps=25, width=640, height=360)
    sid = backend.start(StartOptions()).session_id
    [vw] = list(backend.stream_audio(sid, _audio_window(ms=300)))
    assert vw.frames, "frames list must be non-empty"
    img = Image.open(io.BytesIO(vw.frames[0]))
    assert img.size == (640, 360), f"first frame dims={img.size} != (640,360)"
    assert img.format == "JPEG"


# ---------- animation: frames differ over time ----------


def test_frames_differ_over_time():
    backend = MockRenderBackend(fps=25, width=640, height=360)
    sid = backend.start(StartOptions()).session_id
    # 1 second at 25 fps -> 25 frames
    [vw] = list(backend.stream_audio(sid, _audio_window(ms=1000)))
    frames = vw.frames
    assert len(frames) == 25, f"expected 25 frames, got {len(frames)}"
    # Among the first ~10 frames, at least 2 distinct byte strings (animation).
    head = frames[:10]
    distinct = {bytes(f) for f in head}
    assert len(distinct) >= 2, (
        f"expected >=2 distinct frames in first 10, got {len(distinct)}"
    )


# ---------- get_last_frame_png ----------


def test_get_last_frame_png_decodes_to_configured_dimensions():
    backend = MockRenderBackend(fps=25, width=640, height=360)
    sid = backend.start(StartOptions()).session_id
    list(backend.stream_audio(sid, _audio_window(ms=300)))
    png_bytes = backend.get_last_frame_png(sid)
    assert png_bytes, "png bytes must be non-empty"
    img = Image.open(io.BytesIO(png_bytes))
    assert img.size == (640, 360), f"png dims={img.size} != (640,360)"
    assert img.format == "PNG"


def test_get_last_frame_png_before_any_stream_returns_placeholder():
    """Before any stream_audio call, get_last_frame_png yields a placeholder."""
    backend = MockRenderBackend(fps=25, width=640, height=360)
    sid = backend.start(StartOptions()).session_id
    png_bytes = backend.get_last_frame_png(sid)
    assert png_bytes, "placeholder png must be non-empty"
    img = Image.open(io.BytesIO(png_bytes))
    assert img.size == (640, 360)


# ---------- frame count via num_frames_for ----------


def test_frame_count_matches_num_frames_for_one_second():
    backend = MockRenderBackend(fps=25, width=640, height=360)
    sid = backend.start(StartOptions()).session_id
    aw = _audio_window(ms=1000)
    [vw] = list(backend.stream_audio(sid, aw))
    assert len(vw.frames) == num_frames_for(aw, 25)
