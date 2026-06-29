"""Bounded VideoWindow queue + coordinator metrics (Task 8).

The streaming coordinator (``core.render.orchestrator``) emits ``VideoWindow``
objects as the avatar render stage output. A bounded queue caps how many
unconsumed windows can pile up between the coordinator and the consumer (WS
events / media sink). When the queue is full, the drop-oldest policy evicts
the oldest window and increments a counter — deterministic and simple.

``CoordinatorMetrics`` tracks three signals for the /lite/say response and
for runtime observability:

  - ``first_frame_latency_ms``: time from coordinator start to the first
    VideoWindow emitted (monotonic clock, injectable for tests).
  - ``queue_depth_windows``: current queue depth (updated on each put/get).
  - ``dropped_windows``: total windows dropped due to overflow.

Stdlib only. No asyncio dependency in the metrics class (pure data); the
queue is ``asyncio.Queue``-backed but exposes async put/get.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from .windows import VideoWindow


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

    ``qsize()`` and ``dropped_count()`` are sync diagnostics.

    Args:
        max_size: Maximum number of windows the queue holds before dropping.
            Must be >= 1.
    """

    def __init__(self, max_size: int = 5) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        # ``asyncio.Queue`` with maxsize keeps semantics tight, but its
        # ``put`` BLOCKS when full. We want drop-oldest (non-blocking), so we
        # use a maxsize queue and manually evict before putting when full.
        self._q: asyncio.Queue[VideoWindow] = asyncio.Queue(maxsize=max_size)
        self._dropped = 0

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


# ---------------------------------------------------------------------------
# CoordinatorMetrics
# ---------------------------------------------------------------------------


class CoordinatorMetrics:
    """Tracks coordinator observability signals for one /lite/say turn.

    - ``first_frame_latency_ms``: time from ``record_start()`` to
      ``record_first_frame()``. ``None`` until the first frame is recorded.
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
        self.first_frame_latency_ms: Optional[float] = None
        self.queue_depth_windows: int = 0
        self.dropped_windows: int = 0

    def record_start(self) -> None:
        """Stamp the coordinator start time (called once at run() entry)."""
        self._start = self._clock()

    def record_first_frame(self) -> None:
        """Stamp the first VideoWindow emission and compute latency.

        No-op if called before ``record_start`` or more than once (the first
        frame is the one that matters).
        """
        if self._start is None or self._first_frame_at is not None:
            return
        self._first_frame_at = self._clock()
        self.first_frame_latency_ms = (self._first_frame_at - self._start) * 1000.0

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
            "first_frame_latency_ms": self.first_frame_latency_ms,
            "queue_depth_windows": self.queue_depth_windows,
            "dropped_windows": self.dropped_windows,
        }


__all__ = ["BoundedVideoQueue", "CoordinatorMetrics"]
