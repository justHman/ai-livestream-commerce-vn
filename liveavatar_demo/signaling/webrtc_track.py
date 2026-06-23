"""WebRTC Video Track — wraps the pipeline frame generator into an aiortc MediaStreamTrack.

Receives video frames from the StreamingOrchestrator and delivers them
as WebRTC video frames to connected peers.
"""

from __future__ import annotations

import asyncio
import fractions
import time
from typing import Optional

import av
import numpy as np

try:
    from aiortc import MediaStreamTrack
    from aiortc.contrib.media import MediaStreamError
except ImportError:
    # Stub for when aiortc is not installed
    class MediaStreamTrack:  # type: ignore[no-redef]
        kind = "video"

    class MediaStreamError(Exception):
        pass


class AvatarVideoTrack(MediaStreamTrack):
    """WebRTC video track that streams avatar frames from the pipeline.

    Parameters
    ----------
    frame_generator : callable
        Async generator that yields (frame_np, metadata_dict) tuples.
        frame_np is an (H, W, 3) uint8 numpy array.
    fps : int
        Target frames per second.
    """

    kind = "video"

    def __init__(
        self,
        frame_generator=None,
        fps: int = 24,
    ) -> None:
        super().__init__()
        self._frame_generator = frame_generator
        self._fps = fps
        self._frame_count = 0
        self._start_time: Optional[float] = None
        self._timestamp_base = 0

    async def recv(self) -> "av.VideoFrame":
        """Return the next video frame.

        Called by aiortc's PeerConnection at the track's framerate.
        """
        if self._frame_generator is None:
            raise MediaStreamError("No frame generator configured")

        try:
            frame_data, metadata = await self._frame_generator.__anext__()
        except StopAsyncIteration:
            raise MediaStreamError("Frame generator exhausted")

        if self._start_time is None:
            self._start_time = time.time()

        # Convert numpy RGB to av.VideoFrame
        frame = av.VideoFrame.from_ndarray(frame_data, format="rgb24")

        # Set PTS based on frame count and target FPS
        time_base = fractions.Fraction(1, self._fps)
        frame.pts = self._frame_count
        frame.time_base = time_base

        self._frame_count += 1
        return frame

    @property
    def frame_count(self) -> int:
        return self._frame_count


class FrameQueue:
    """Thread-safe async queue for passing frames from pipeline to WebRTC track.

    The orchestrator (sync) pushes frames, the WebRTC track (async) pulls them.
    """

    def __init__(self, maxsize: int = 30) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    def put_frame(self, frame: np.ndarray, metadata: dict) -> None:
        """Push a frame (called from sync pipeline thread)."""
        if self._closed:
            return
        try:
            self._queue.put_nowait((frame, metadata))
        except asyncio.QueueFull:
            # Drop oldest frame to keep latency low
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait((frame, metadata))

    async def get_frame(self) -> tuple[np.ndarray, dict]:
        """Pull a frame (called from async WebRTC track)."""
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    def close(self) -> None:
        self._closed = True

    async def async_generator(self):
        """Yield frames as an async generator (for AvatarVideoTrack)."""
        while not self._closed:
            try:
                frame, metadata = await asyncio.wait_for(
                    self.get_frame(), timeout=5.0
                )
                yield frame, metadata
            except asyncio.TimeoutError:
                if self._closed:
                    break
