"""Bounded VideoWindow queue + coordinator metrics (Task 8).

The streaming coordinator (``backend.application.render.orchestrator``) emits ``VideoWindow``
objects as the avatar render stage output. A bounded queue caps how many
unconsumed windows can pile up between the coordinator and the consumer (WS
events / media sink). When the queue is full, the drop-oldest policy evicts
the oldest window and increments a counter — deterministic and simple.

``CoordinatorMetrics`` tracks three signals for the /lite/say response and
for runtime observability:

  - ``pipeline_total_ms``: time from coordinator start to the FIRST
    ``VideoWindow`` being put into the async queue. Phase E's streaming-drain
    bridge makes this a real first-window latency proxy.
  - ``queue_depth_windows``: current queue depth (updated on each put/get).
  - ``dropped_windows``: total windows dropped due to overflow.

Stdlib only. No asyncio dependency in the metrics class (pure data); the
queue is ``asyncio.Queue``-backed but exposes async put/get.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from .engines.windows import VideoWindow


# ---------------------------------------------------------------------------
# BoundedVideoQueue
# ---------------------------------------------------------------------------


class BoundedVideoQueue:
    """An asyncio queue of ``VideoWindow`` with a hard max size and drop-oldest
    overflow policy.

    ``put(window)`` is async and never blocks: if the queue is full, the
    OLDEST window is evicted (and the dropped counter incremented) before the
    new window is enqueued. Returns True if the window was put without a drop,
    False if a drop occurred.

    ``get()`` is async and blocks until a window is available.

    ``get_or_idle(idle_provider, timeout_ms)`` is async and returns one JPEG
    frame per call: drains frames from the current VideoWindow first, then
    blocks up to ``timeout_ms`` for the next VideoWindow, and on timeout falls
    back to ``idle_provider()`` (used by the continuous MJPEG endpoint so the
    frontend never sees a black/frozen frame).

    ``qsize()`` and ``dropped_count()`` are sync diagnostics. Idle-related
    counters (``idle_frames_served``, ``underflow_count``, ``last_frame_age_ms``)
    expose how often the queue ran dry and how stale the most recent emitted
    frame is.

    Args:
        max_size: Maximum number of windows the queue holds before dropping.
            Must be >= 1.
        clock: Monotonic clock source (seconds). Injectable for tests.
    """

    def __init__(
        self,
        max_size: int = 5,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        # ``asyncio.Queue`` with maxsize keeps semantics tight, but its
        # ``put`` BLOCKS when full. We want drop-oldest (non-blocking), so we
        # use a maxsize queue and manually evict before putting when full.
        self._q: asyncio.Queue[VideoWindow] = asyncio.Queue(maxsize=max_size)
        self._dropped = 0
        # Per-frame cursor over the most recently dequeued VideoWindow. The
        # mjpeg endpoint and tests consume one JPEG at a time via
        # ``get_or_idle``, so we drain ``current.frames`` before pulling the
        # next window off the queue.
        self._current: Optional[VideoWindow] = None
        self._cursor: int = 0
        # Idle / age metrics (per Phase C plan).
        self.idle_frames_served: int = 0
        self.underflow_count: int = 0
        self.last_frame_age_ms: float = 0.0
        # Last successfully returned frame (true or idle); used as emergency
        # fallback if both the queue is empty AND ``idle_provider`` raises.
        self.last_frame: Optional[bytes] = None
        self._last_frame_at: Optional[float] = None
        self._clock = clock

    @property
    def max_size(self) -> int:
        return self._max_size

    async def put(self, window: VideoWindow) -> bool:
        """Put ``window`` into the queue, dropping the oldest if full.

        Returns True if put without a drop, False if a drop occurred.
        Never blocks.
        """
        dropped = False
        if self._q.full():
            # Evict the oldest (FIFO) to make room. ``get_nowait`` is safe
            # because ``full()`` was True.
            try:
                self._q.get_nowait()
                self._dropped += 1
                dropped = True
            except asyncio.QueueEmpty:  # pragma: no cover - race-safe guard
                pass
        # ``put_nowait`` is safe now (we made room above; queue was not full
        # or we evicted one). Use the sync put to avoid an unnecessary await
        # since we have guaranteed capacity.
        self._q.put_nowait(window)
        return not dropped

    async def get(self) -> VideoWindow:
        """Blocking get. Returns the next VideoWindow in FIFO order."""
        return await self._q.get()

    def qsize(self) -> int:
        """Current number of windows in the queue."""
        return self._q.qsize()

    def dropped_count(self) -> int:
        """Total windows dropped due to overflow since the queue was created."""
        return self._dropped

    def clear(self) -> None:
        """Drain all pending windows (used on cancel). Sync, non-blocking."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race-safe guard
                break
        self._current = None
        self._cursor = 0

    async def get_or_idle(
        self,
        idle_provider: Callable[[], bytes],
        timeout_ms: int = 40,
    ) -> tuple[bytes, bool]:
        """Return one JPEG frame; fall back to ``idle_provider()`` on timeout.

        Per-frame cursor semantics:
          1. If there is a current VideoWindow with remaining frames, pop the
             next frame and return it as ``(jpeg, False)`` (queue-served frame).
          2. Otherwise, wait up to ``timeout_ms`` for the next VideoWindow to
             arrive via ``put``; on success, install it as current and emit
             its first frame as ``(jpeg, False)``.
          3. On timeout, call ``idle_provider()`` and return ``(jpeg, True)``
             — increments ``idle_frames_served`` and ``underflow_count``.

        Emergency fallback: if both the queue is empty AND ``idle_provider``
        raises, the most recently emitted frame in ``self.last_frame`` is
        re-emitted (still as ``is_idle=True``) so the stream never gaps. If
        no frame has ever been emitted, the original exception is re-raised.

        ``last_frame_age_ms`` is updated on every successful call (time since
        the previous emitted frame at this call's clock).
        """
        now = self._clock()
        # Update age before we possibly block — callers want time since the
        # last emit at the moment they asked, not at the moment we returned.
        if self._last_frame_at is not None:
            self.last_frame_age_ms = (now - self._last_frame_at) * 1000.0

        # 1. Frame still in current window?
        if self._current is not None and self._cursor < len(self._current.frames):
            jpeg = self._current.frames[self._cursor]
            self._cursor += 1
            return self._after_emit(jpeg, is_idle=False)

        # 2. Wait briefly for the next VideoWindow.
        try:
            window = await asyncio.wait_for(self._q.get(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            window = None

        if window is not None and window.frames:
            self._current = window
            self._cursor = 1
            return self._after_emit(window.frames[0], is_idle=False)

        # 3. Idle fallback.
        self.underflow_count += 1
        try:
            jpeg = idle_provider()
        except Exception:
            if self.last_frame is not None:
                self.idle_frames_served += 1
                return self._after_emit(self.last_frame, is_idle=True)
            raise
        self.idle_frames_served += 1
        return self._after_emit(jpeg, is_idle=True)

    def _after_emit(self, jpeg: bytes, *, is_idle: bool) -> tuple[bytes, bool]:
        """Update ``last_frame`` + ``_last_frame_at`` after every successful emit."""
        self.last_frame = jpeg
        self._last_frame_at = self._clock()
        return jpeg, is_idle


# ---------------------------------------------------------------------------
# CoordinatorMetrics
# ---------------------------------------------------------------------------


class CoordinatorMetrics:
    """Tracks coordinator observability signals for one /lite/say turn.

    - ``pipeline_total_ms``: time from ``record_start()`` to the first
      ``VideoWindow`` being put into the async queue. Phase E's streaming-drain
      bridge records this while the sync worker is still producing later
      windows, so it is a real first-window latency proxy.
    - ``queue_depth_windows``: last reported queue depth (caller updates via
      ``update_queue_depth``).
    - ``dropped_windows``: total dropped windows (caller increments via
      ``increment_dropped``; also reflected from the queue when overflow
      happens — caller chooses which source is authoritative).

    The clock is injectable (defaults to ``time.monotonic``) for deterministic
    latency assertions in tests. The clock returns seconds; latencies are
    reported in milliseconds.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start: Optional[float] = None
        self._first_frame_at: Optional[float] = None
        self.pipeline_total_ms: Optional[float] = None
        self.queue_depth_windows: int = 0
        self.dropped_windows: int = 0

    def record_start(self) -> None:
        """Stamp the coordinator start time (called once at run() entry)."""
        self._start = self._clock()

    def record_first_frame(self) -> None:
        """Stamp the first-window time and compute latency in ms.

        No-op if called before ``record_start`` or more than once.
        """
        if self._start is None or self._first_frame_at is not None:
            return
        self._first_frame_at = self._clock()
        self.pipeline_total_ms = (self._first_frame_at - self._start) * 1000.0

    def update_queue_depth(self, depth: int) -> None:
        """Record the current queue depth (call after each put/get)."""
        self.queue_depth_windows = depth

    def increment_dropped(self, n: int = 1) -> None:
        """Increment the dropped-windows counter by ``n`` (default 1)."""
        if n < 0:
            raise ValueError(f"increment must be >= 0, got {n}")
        self.dropped_windows += n

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict for the /lite/say response."""
        return {
            "pipeline_total_ms": self.pipeline_total_ms,
            "queue_depth_windows": self.queue_depth_windows,
            "dropped_windows": self.dropped_windows,
        }


__all__ = ["BoundedVideoQueue", "CoordinatorMetrics"]
