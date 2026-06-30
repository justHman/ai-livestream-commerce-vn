"""Per-session chat queue for continuous comment ingestion (Phase B).

Thread-safe bounded deque of incoming comments. The coordinator's tick loop
calls ``drain_window()`` to peek at the rolling window; comments age out via
the window filter + max_size eviction, NOT single-shot drain.

Public surface:
  IncomingComment    — frozen dataclass for one ingested viewer comment
  ChatQueue
    put(text, author, ts?) -> IncomingComment
    drain_window(window_sec) -> list[IncomingComment]
    stats() -> dict
    clear() -> None
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class IncomingComment:
    """One viewer comment as ingested from the API."""

    text: str
    author: str
    ts: float       # unix seconds
    id: str          # uuid4 hex, assigned at put()


class ChatQueue:
    """Bounded, thread-safe queue for one live session's chat comments.

    ``put()`` appends; if the deque exceeds ``max_size``, the oldest comment
    is silently dropped (back-pressure on the viewer side is not needed for
    a livestream chat — we just keep the freshest N).

    ``drain_window()`` returns a snapshot of comments within a rolling time
    window. It does NOT remove them; the coordinator may tick multiple times
    within the same window.
    """

    def __init__(self, session_id: str, max_size: int = 500) -> None:
        self.session_id = session_id
        self.max_size = max_size
        self._deque: deque[IncomingComment] = deque()
        self._lock = threading.Lock()
        self._total_put: int = 0

    def put(
        self,
        text: str,
        author: str,
        ts: Optional[float] = None,
    ) -> IncomingComment:
        """Append a comment. Returns the created ``IncomingComment``.

        Thread-safe. If the deque exceeds ``max_size``, the oldest entry is
        dropped.
        """
        comment = IncomingComment(
            text=text,
            author=author,
            ts=ts if ts is not None else time.time(),
            id=uuid.uuid4().hex,
        )
        with self._lock:
            self._deque.append(comment)
            self._total_put += 1
            if len(self._deque) > self.max_size:
                self._deque.popleft()
        return comment

    def drain_window(self, window_sec: float = 75.0) -> list[IncomingComment]:
        """Return comments whose ``ts >= now - window_sec``.

        This is a non-destructive peek: the same comment may appear in
        consecutive drain_window calls until it ages out or is evicted by
        max_size.
        """
        cutoff = time.time() - window_sec
        with self._lock:
            return [c for c in self._deque if c.ts >= cutoff]

    def stats(self) -> dict:
        """Diagnostic snapshot: pending count and age of oldest comment."""
        with self._lock:
            n = len(self._deque)
            if n == 0:
                return {"pending": 0, "oldest_ms_ago": None, "total_put": self._total_put}
            oldest_ts = self._deque[0].ts
        age_ms = (time.time() - oldest_ts) * 1000.0
        return {"pending": n, "oldest_ms_ago": round(age_ms, 1), "total_put": self._total_put}

    def clear(self) -> None:
        """Drop all comments (used on session stop)."""
        with self._lock:
            self._deque.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)


__all__ = ["IncomingComment", "ChatQueue"]
