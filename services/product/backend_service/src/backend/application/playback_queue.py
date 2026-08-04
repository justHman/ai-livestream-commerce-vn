"""Canonical playback queue (OpenSpec 1.21).

Owns backpressure and cancellation for the LLM -> chunking -> TTS -> avatar
playback pipeline. Bounded deque with a fixed window cap: when the deque
exceeds ``max_windows``, the oldest window is dropped (backpressure policy:
keep the freshest N). Cancellation is cooperative — ``cancel()`` sets a flag
the producers check between windows.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

__all__ = ["PlaybackItem", "PlaybackQueue", "PlaybackCancelled"]


class PlaybackCancelled(RuntimeError):
    """Raised by producers when the queue was cancelled mid-utterance."""


@dataclass(frozen=True)
class PlaybackItem:
    """One queued playback window with its turn identity."""

    turn_id: str
    seq: int
    payload: object  # AudioWindow / VideoWindow from the avatar pipeline
    is_final: bool = False


class PlaybackQueue:
    """Thread-safe bounded queue for one session's playback windows."""

    def __init__(self, session_id: str, max_windows: int = 5) -> None:
        if max_windows < 1:
            raise ValueError("max_windows must be positive")
        self.session_id = session_id
        self.max_windows = max_windows
        self._deque: deque[PlaybackItem] = deque()
        self._lock = threading.Lock()
        self._cancelled = False
        self._put_total = 0

    def put(self, item: PlaybackItem) -> PlaybackItem:
        """Append one window; drop the oldest when over capacity."""
        with self._lock:
            if self._cancelled:
                raise PlaybackCancelled(self.session_id)
            self._deque.append(item)
            self._put_total += 1
            if len(self._deque) > self.max_windows:
                self._deque.popleft()
        return item

    def get(self) -> Optional[PlaybackItem]:
        """Pop the next window, or None when empty."""
        with self._lock:
            if not self._deque:
                return None
            return self._deque.popleft()

    def peek(self) -> Optional[PlaybackItem]:
        with self._lock:
            return self._deque[0] if self._deque else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)

    @property
    def size(self) -> int:
        return len(self)

    @property
    def put_total(self) -> int:
        with self._lock:
            return self._put_total

    def cancel(self) -> None:
        """Cooperatively cancel: producers raise PlaybackCancelled on put."""
        with self._lock:
            self._cancelled = True
            self._deque.clear()

    def reset(self) -> None:
        """Re-arm after cancellation (new utterance on the same queue)."""
        with self._lock:
            self._cancelled = False
            self._deque.clear()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


def new_item(turn_id: str, seq: int, payload: object, *, is_final: bool = False) -> PlaybackItem:
    return PlaybackItem(turn_id=turn_id, seq=seq, payload=payload, is_final=is_final)
