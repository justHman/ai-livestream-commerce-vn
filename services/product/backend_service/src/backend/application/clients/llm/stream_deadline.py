"""Per-request streaming cancellation and deadlines.

Deliberately thread-free: deadlines are checked between SSE lines in the reader's own
thread and a blocked read is bounded by the httpx read timeout, so the request closes
through its own context manager (never a socket shutdown from another thread).
"""

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

import httpx


TOTAL_TIMEOUT_S = 60.0
FIRST_CONTENT_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 5.0
_CANCEL: ContextVar[threading.Event | None] = ContextVar("llm_cancel", default=None)


@contextmanager
def streaming_cancellation(event):
    token = _CANCEL.set(event)
    try:
        yield
    finally:
        _CANCEL.reset(token)


class StreamDeadline:
    """One per attempt (a retry builds a fresh one)."""

    def __init__(self, total, first):
        now = time.monotonic()
        self.total, self.first = now + total, now + first
        self.content = threading.Event()
        self.cancel = _CANCEL.get()

    def check(self):
        now = time.monotonic()
        if (
            (self.cancel is not None and self.cancel.is_set())
            or now >= self.total
            or (not self.content.is_set() and now >= self.first)
        ):
            raise TimeoutError("LLM streaming deadline/cancellation exceeded")

    def http_timeout(self, read_timeout):
        """connect/pool/write short; read bounded by what is left of the active deadline."""
        limit = self.total if self.content.is_set() else min(self.first, self.total)
        budget = max(0.001, limit - time.monotonic())
        short = min(CONNECT_TIMEOUT_S, budget)
        return httpx.Timeout(connect=short, write=short, pool=short, read=min(read_timeout, budget))
