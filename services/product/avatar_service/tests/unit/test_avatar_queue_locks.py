"""Unit tests for the bounded video queue, coordinator metrics and session locks."""

from __future__ import annotations

import pytest

from avatar.engines.windows import VideoWindow
from avatar.locks import SessionLockRegistry
from avatar.queue import BoundedVideoQueue, CoordinatorMetrics


def _window(session_id: str = "s1", seq: int = 0, frames: list[bytes] | None = None) -> VideoWindow:
    return VideoWindow(
        session_id=session_id,
        utterance_id="u1",
        seq=seq,
        frames=frames if frames is not None else [b"\x00\x00" * 100],
        fps=30,
        duration_ms=33,
        audio_window_id="a1",
        is_final=False,
    )


# --- SessionLockRegistry ---------------------------------------------------


def test_lock_registry_get_creates_and_reuses():
    reg = SessionLockRegistry()
    assert reg.get("s1") is reg.get("s1")
    assert not reg.is_locked("s1")


def test_lock_registry_try_acquire_rejects_second_holder():
    reg = SessionLockRegistry()
    assert reg.try_acquire("s1") is True
    assert reg.is_locked("s1") is True
    # Second acquire on the same session fails (non-blocking).
    assert reg.try_acquire("s1") is False


def test_lock_registry_release_frees_and_double_release_is_noop():
    reg = SessionLockRegistry()
    assert reg.try_acquire("s1") is True
    reg.release("s1")
    assert not reg.is_locked("s1")
    # Double release / release-when-free is a no-op (no RuntimeError).
    reg.release("s1")


def test_lock_registry_release_without_entry_is_noop():
    reg = SessionLockRegistry()
    reg.release("missing")  # must not raise
    assert not reg.is_locked("missing")


def test_lock_registry_drop_removes_entry():
    reg = SessionLockRegistry()
    reg.get("s1")
    reg.drop("s1")
    assert not reg.is_locked("s1")
    # Dropping again / dropping a never-seen session is safe.
    reg.drop("s1")
    reg.drop("missing")


def test_lock_registry_sessions_are_independent():
    reg = SessionLockRegistry()
    assert reg.try_acquire("s1") is True
    assert reg.try_acquire("s2") is True  # different session, not blocked
    reg.release("s1")
    reg.release("s2")


# --- BoundedVideoQueue -----------------------------------------------------


def test_queue_rejects_invalid_max_size():
    with pytest.raises(ValueError):
        BoundedVideoQueue(max_size=0)


async def test_queue_put_get_roundtrip():
    q = BoundedVideoQueue(max_size=2)
    win = _window()
    assert await q.put(win) is True
    assert q.qsize() == 1
    assert await q.get() is win
    assert q.qsize() == 0
    assert q.dropped_count() == 0


async def test_queue_drops_oldest_when_full():
    q = BoundedVideoQueue(max_size=2)
    w1 = _window(seq=1)
    w2 = _window(seq=2)
    w3 = _window(seq=3)
    assert await q.put(w1) is True
    assert await q.put(w2) is True
    # Third put on a full queue drops the oldest (w1).
    assert await q.put(w3) is False
    assert q.dropped_count() == 1
    assert q.qsize() == 2
    assert await q.get() is w2  # oldest surviving is w2, not w1


async def test_queue_clear_drains_pending():
    q = BoundedVideoQueue(max_size=3)
    for i in range(3):
        await q.put(_window(seq=i))
    q.clear()
    assert q.qsize() == 0
    # clear is idempotent.
    q.clear()


async def test_queue_get_or_idle_serves_frames_then_idle():
    q = BoundedVideoQueue(max_size=2)
    frame_a = b"frame-a"
    frame_b = b"frame-b"
    await q.put(_window(frames=[frame_a, frame_b]))
    jpeg, is_idle = await q.get_or_idle(lambda: b"idle")
    assert jpeg == frame_a and is_idle is False
    # Second frame comes from the same window cursor.
    jpeg, is_idle = await q.get_or_idle(lambda: b"idle")
    assert jpeg == frame_b and is_idle is False
    # Window exhausted -> timeout -> idle fallback.
    jpeg, is_idle = await q.get_or_idle(lambda: b"idle", timeout_ms=5)
    assert jpeg == b"idle" and is_idle is True
    assert q.idle_frames_served == 1
    assert q.underflow_count == 1


async def test_queue_get_or_idle_emergency_fallback_reuses_last_frame():
    q = BoundedVideoQueue(max_size=2)
    # Seed last_frame with a real emit first.
    await q.put(_window(frames=[b"real"]))
    await q.get_or_idle(lambda: b"unused", timeout_ms=1)

    def boom():
        raise RuntimeError("provider down")

    jpeg, is_idle = await q.get_or_idle(boom, timeout_ms=1)
    assert jpeg == b"real" and is_idle is True
    assert q.idle_frames_served >= 1


async def test_queue_get_or_idle_re_raises_when_no_last_frame():
    q = BoundedVideoQueue(max_size=2)
    with pytest.raises(RuntimeError, match="provider down"):
        await q.get_or_idle(
            lambda: (_ for _ in ()).throw(RuntimeError("provider down")), timeout_ms=1
        )


async def test_queue_tracks_last_frame_age():
    # Each get_or_idle reads the clock twice: once for the age check and once
    # in _after_emit. Emit 1 happens at t=1.5, then the next call checks age
    # at t=1.7 and re-emits at t=1.9.
    clock = iter([1.0, 1.5, 1.7, 1.9])
    q = BoundedVideoQueue(max_size=2, clock=lambda: next(clock))
    await q.put(_window(frames=[b"f"]))
    await q.get_or_idle(lambda: b"i", timeout_ms=1)  # emit at t=1.5
    assert q.last_frame == b"f"
    await q.get_or_idle(lambda: b"i", timeout_ms=1)  # checks age at t=1.7
    assert q.last_frame_age_ms == pytest.approx(200.0)


# --- CoordinatorMetrics ----------------------------------------------------


def test_metrics_records_pipeline_latency():
    clock = iter([10.0, 10.25])
    m = CoordinatorMetrics(clock=lambda: next(clock))
    m.record_start()
    m.record_first_frame()
    assert m.pipeline_total_ms == 250.0


def test_metrics_first_frame_before_start_or_twice_is_noop():
    m = CoordinatorMetrics(clock=lambda: 1.0)
    m.record_first_frame()  # no start yet -> no-op
    assert m.pipeline_total_ms is None
    m.record_start()
    m.record_first_frame()
    first = m.pipeline_total_ms
    m.record_first_frame()  # already recorded -> no-op
    assert m.pipeline_total_ms == first


def test_metrics_depth_and_dropped():
    m = CoordinatorMetrics()
    m.update_queue_depth(3)
    assert m.queue_depth_windows == 3
    m.increment_dropped()
    m.increment_dropped(2)
    assert m.dropped_windows == 3


def test_metrics_increment_rejects_negative():
    m = CoordinatorMetrics()
    with pytest.raises(ValueError):
        m.increment_dropped(-1)


def test_metrics_to_dict_serializes():
    m = CoordinatorMetrics()
    m.record_start()
    m.record_first_frame()
    m.update_queue_depth(1)
    m.increment_dropped()
    d = m.to_dict()
    assert set(d) == {"pipeline_total_ms", "queue_depth_windows", "dropped_windows"}
    assert d["queue_depth_windows"] == 1
    assert d["dropped_windows"] == 1
