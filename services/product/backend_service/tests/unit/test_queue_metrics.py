"""Unit tests for BoundedVideoQueue.get_or_idle + idle metrics (Phase C).

Covers:
  - get_or_idle returns ``(idle_jpeg, True)`` on empty-queue timeout;
    ``idle_frames_served`` and ``underflow_count`` both increment.
  - get_or_idle returns ``(queue_jpeg, False)`` when a VideoWindow is queued;
    idle counters unchanged.
  - last_frame is updated on every successful emit (idle or real).
  - Emergency fallback: if idle_provider raises and last_frame is set, the
    last frame is re-emitted (still marked idle).
  - Per-frame cursor: a VideoWindow with N frames yields N consecutive
    non-idle frames before the next queue.get is attempted.
  - last_frame_age_ms updates monotonically while no new frame arrives.

Offline: stdlib + asyncio only.
"""

from __future__ import annotations


import pytest

from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import VideoWindow


def _window(
    seq: int = 0, *, frames: list[bytes] | None = None, is_final: bool = False
) -> VideoWindow:
    return VideoWindow(
        session_id="sess-test",
        utterance_id="utt-1",
        seq=seq,
        frames=frames if frames is not None else [f"queue-frame-{seq}".encode()],
        fps=25,
        duration_ms=40,
        audio_window_id="aw-1",
        is_final=is_final,
    )


@pytest.mark.asyncio
async def test_get_or_idle_returns_idle_on_timeout_with_empty_queue():
    q = BoundedVideoQueue(max_size=5)
    jpeg, is_idle = await q.get_or_idle(idle_provider=lambda: b"idle-jpeg", timeout_ms=10)
    assert jpeg == b"idle-jpeg"
    assert is_idle is True
    assert q.idle_frames_served == 1
    assert q.underflow_count == 1
    assert q.last_frame == b"idle-jpeg"


@pytest.mark.asyncio
async def test_get_or_idle_returns_queue_frame_when_available():
    q = BoundedVideoQueue(max_size=5)
    await q.put(_window(0, frames=[b"real-frame"]))
    jpeg, is_idle = await q.get_or_idle(idle_provider=lambda: b"idle-jpeg", timeout_ms=10)
    assert jpeg == b"real-frame"
    assert is_idle is False
    # Idle counters unchanged.
    assert q.idle_frames_served == 0
    assert q.underflow_count == 0
    assert q.last_frame == b"real-frame"


@pytest.mark.asyncio
async def test_get_or_idle_drains_multi_frame_window_via_cursor():
    """A VideoWindow with N frames should yield N consecutive non-idle frames."""
    q = BoundedVideoQueue(max_size=5)
    frames = [b"f0", b"f1", b"f2"]
    await q.put(_window(0, frames=frames))

    out = []
    for _ in range(3):
        out.append(await q.get_or_idle(idle_provider=lambda: b"idle", timeout_ms=10))

    assert [f for f, _ in out] == frames
    assert [is_idle for _, is_idle in out] == [False, False, False]
    assert q.idle_frames_served == 0


@pytest.mark.asyncio
async def test_get_or_idle_falls_back_to_idle_after_window_exhausted():
    """After draining a window, the next call should idle (queue empty)."""
    q = BoundedVideoQueue(max_size=5)
    await q.put(_window(0, frames=[b"only-frame"]))

    jpeg1, idle1 = await q.get_or_idle(idle_provider=lambda: b"idle", timeout_ms=10)
    jpeg2, idle2 = await q.get_or_idle(idle_provider=lambda: b"idle", timeout_ms=10)

    assert (jpeg1, idle1) == (b"only-frame", False)
    assert (jpeg2, idle2) == (b"idle", True)
    assert q.idle_frames_served == 1
    assert q.underflow_count == 1


@pytest.mark.asyncio
async def test_get_or_idle_emergency_fallback_to_last_frame_on_provider_failure():
    """If idle_provider raises but a last_frame exists, re-emit it (still idle)."""
    q = BoundedVideoQueue(max_size=5)
    # Prime with one real frame so last_frame is set.
    await q.put(_window(0, frames=[b"primer"]))
    await q.get_or_idle(idle_provider=lambda: b"unused", timeout_ms=10)
    assert q.last_frame == b"primer"

    def boom() -> bytes:
        raise RuntimeError("idle provider exploded")

    jpeg, is_idle = await q.get_or_idle(idle_provider=boom, timeout_ms=10)
    assert jpeg == b"primer"
    assert is_idle is True
    # underflow still increments because the queue was empty.
    assert q.underflow_count == 1
    assert q.idle_frames_served == 1


@pytest.mark.asyncio
async def test_get_or_idle_emergency_fallback_re_raises_when_no_last_frame():
    q = BoundedVideoQueue(max_size=5)

    def boom() -> bytes:
        raise RuntimeError("idle provider exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        await q.get_or_idle(idle_provider=boom, timeout_ms=10)


@pytest.mark.asyncio
async def test_last_frame_age_ms_updates_between_calls():
    """last_frame_age_ms reflects time since the previous emit at the next call."""
    times = [100.0]
    q = BoundedVideoQueue(max_size=5, clock=lambda: times[0])

    # First call: no prior frame; age stays at 0.
    await q.get_or_idle(idle_provider=lambda: b"idle-1", timeout_ms=10)
    assert q.last_frame_age_ms == 0.0
    assert q.last_frame == b"idle-1"

    # Advance the clock and make a second call.
    times[0] = 100.150  # +150 ms
    await q.get_or_idle(idle_provider=lambda: b"idle-2", timeout_ms=10)
    assert q.last_frame_age_ms == pytest.approx(150.0, abs=1.0)
    assert q.last_frame == b"idle-2"


@pytest.mark.asyncio
async def test_clear_resets_cursor():
    """After clear(), a previously-installed current window is dropped."""
    q = BoundedVideoQueue(max_size=5)
    await q.put(_window(0, frames=[b"a", b"b", b"c"]))
    # Pull one frame so cursor advances.
    jpeg, _ = await q.get_or_idle(idle_provider=lambda: b"idle", timeout_ms=10)
    assert jpeg == b"a"
    q.clear()
    # After clear, next call should idle (cursor + queue both reset).
    jpeg, is_idle = await q.get_or_idle(idle_provider=lambda: b"idle", timeout_ms=10)
    assert (jpeg, is_idle) == (b"idle", True)


@pytest.mark.asyncio
async def test_existing_metrics_unaffected_by_idle_path():
    """Sanity: pre-existing put/get/dropped behavior must still work."""
    q = BoundedVideoQueue(max_size=2)
    await q.put(_window(0))
    await q.put(_window(1))
    # Overflow drops oldest.
    result = await q.put(_window(2))
    assert result is False
    assert q.dropped_count() == 1
    assert q.qsize() == 2
    # CoordinatorMetrics still serializes fine.
    m = CoordinatorMetrics()
    m.record_start()
    m.record_first_frame()
    m.update_queue_depth(q.qsize())
    m.increment_dropped(q.dropped_count())
    d = m.to_dict()
    assert d["queue_depth_windows"] == 2
    assert d["dropped_windows"] == 1
