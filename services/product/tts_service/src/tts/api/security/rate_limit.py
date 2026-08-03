"""Rate and concurrency limiting for the TTS service (GPU-aware)."""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, status


class RateLimitError(HTTPException):
    def __init__(self, detail: str = "rate limit exceeded") -> None:
        super().__init__(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


class ConcurrencyLimitError(HTTPException):
    def __init__(self, detail: str = "concurrency limit exceeded") -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        )


class RateLimiter:
    """Fixed-window rate limiter with monotonic clock."""

    def __init__(self, *, max_requests: int, window_sec: float = 60.0) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        self._max = max_requests
        self._window = window_sec
        self._lock = threading.Lock()
        self._count = 0
        self._window_start = time.monotonic()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self._window:
                self._window_start = now
                self._count = 0
            if self._count >= self._max:
                return False
            self._count += 1
            return True


class ConcurrencyLimiter:
    """Bounded concurrent-slot semaphore acquired in a route dependency."""

    def __init__(self, *, max_concurrent: int = 4) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    def acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._semaphore.release()
        except ValueError:
            pass

    def __enter__(self) -> "ConcurrencyLimiter":
        if not self.acquire():
            raise ConcurrencyLimitError("no concurrency slot available")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class GPUConcurrencyLimiter(ConcurrencyLimiter):
    """Concurrency limiter defaulting to a single GPU request at a time."""

    def __init__(self, *, max_gpu_concurrent: int = 1) -> None:
        super().__init__(max_concurrent=max_gpu_concurrent)