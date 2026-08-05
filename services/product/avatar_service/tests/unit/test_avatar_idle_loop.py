"""Unit tests for MockRenderBackend idle loop (Phase C).

Covers:
  - start() pre-renders the idle loop cache with ``_idle_loop_len`` frames
    (75 frames @ 25 fps by default).
  - get_idle_frame_jpeg(t_ms=0) and get_idle_frame_jpeg(t_ms=40) return
    different bytes (frame 0 vs frame 1).
  - get_idle_frame_jpeg(t_ms=0) == get_idle_frame_jpeg(t_ms=75*40) (loop wrap).
  - iter_idle_frames yields enough frames to loop at least twice.
  - stop() removes the session entry (cache evicted with it).
  - get_idle_frame_jpeg / iter_idle_frames raise KeyError for unknown sessions.

Offline: PIL only, no network, no model downloads.
"""

from __future__ import annotations

import itertools

import pytest

from avatar.engines.base import StartOptions
from avatar.engines.mock import MockRenderBackend


def test_start_prerenders_idle_loop_75_frames_at_25fps():
    backend = MockRenderBackend(fps=25)
    res = backend.start(StartOptions())
    sid = res.session_id

    sess = backend._sessions[sid]
    # 25 fps * 3 s = 75 frames.
    assert backend._idle_loop_len == 75
    assert len(sess.idle_loop_frames) == 75
    # Every frame is non-empty JPEG bytes.
    assert all(isinstance(f, (bytes, bytearray)) and len(f) > 0 for f in sess.idle_loop_frames)


def test_get_idle_frame_jpeg_deterministic_by_time():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id

    f0a = backend.get_idle_frame_jpeg(sid, t_ms=0)
    f0b = backend.get_idle_frame_jpeg(sid, t_ms=0)
    f1 = backend.get_idle_frame_jpeg(sid, t_ms=40)
    f2 = backend.get_idle_frame_jpeg(sid, t_ms=80)

    # Same logical time -> identical bytes.
    assert f0a == f0b
    # 40 ms apart -> next idle frame (different bytes).
    assert f0a != f1
    assert f1 != f2


def test_get_idle_frame_jpeg_wraps_seamlessly_at_loop_length():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id

    # frame 0 ~ frame loop_len (75) ~ frame 2*loop_len (150).
    loop_ms = 75 * 40
    f0 = backend.get_idle_frame_jpeg(sid, t_ms=0)
    f_wrap = backend.get_idle_frame_jpeg(sid, t_ms=loop_ms)
    f_wrap_x2 = backend.get_idle_frame_jpeg(sid, t_ms=2 * loop_ms)

    assert f0 == f_wrap, "idle loop must wrap seamlessly at loop_len"
    assert f0 == f_wrap_x2


def test_get_idle_frame_jpeg_no_t_ms_uses_monotonic_clock():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id
    jpeg = backend.get_idle_frame_jpeg(sid)
    # Just confirm it returns one of the cached frames.
    assert jpeg in backend._sessions[sid].idle_loop_frames


def test_iter_idle_frames_loops_at_least_twice():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id

    it = backend.iter_idle_frames(sid)
    frames = list(itertools.islice(it, 150))  # exactly 2 loops at 75 frames.
    assert len(frames) == 150
    # First 75 frames and second 75 frames should match exactly (loop wrap).
    assert frames[:75] == frames[75:]


def test_stop_removes_idle_loop_cache():
    backend = MockRenderBackend(fps=25)
    sid = backend.start(StartOptions()).session_id
    # Cache exists before stop.
    assert sid in backend._sessions
    backend.stop(sid)
    # Session (and its idle_loop_frames) is gone.
    assert sid not in backend._sessions
    with pytest.raises(KeyError):
        backend.get_idle_frame_jpeg(sid, t_ms=0)
    with pytest.raises(KeyError):
        next(backend.iter_idle_frames(sid))


def test_get_idle_frame_jpeg_unknown_session_raises_keyerror():
    backend = MockRenderBackend(fps=25)
    with pytest.raises(KeyError):
        backend.get_idle_frame_jpeg("no-such-session", t_ms=0)
