"""Active-session log file seam: one path per service, truncate/append/retain.

Task 1.7 owns this module: a logging.Handler writing to a single
`{group}/<service>.log` under an active-sessions root. Session ids never appear
in paths, and service/group names come from fixed allowlists.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TextIO

_PRODUCT_SERVICES = frozenset({"backend", "llm", "tts", "avatar"})
_PLATFORM_SERVICES = frozenset({"livekit", "lmcache", "postgres", "redis"})
_GROUPS = {"product": _PRODUCT_SERVICES, "platform": _PLATFORM_SERVICES}

INACTIVE = "inactive"
ACTIVE = "active"
CLOSED = "closed"


class ActiveSessionHandler(logging.Handler):
    """Write active-session records to `{active_root}/{group}/<service>.log`.

    Lifetime is explicit: constructed inactive; start_session activates and
    truncates; emit appends only while active; end_session deactivates and
    retains the file; close is terminal. All state transitions and writes
    share the inherited handler lock (RLock), so a concurrent emit can never
    interleave with a session-start truncate.
    """

    def __init__(
        self,
        service: str,
        group: str = "product",
        active_root: Path | str = ".runtime/logs/active-sessions",
    ) -> None:
        super().__init__()
        services = _GROUPS.get(group)
        if services is None:
            raise ValueError(f"Unknown active-session group={group!r}; expected product|platform")
        if service not in services:
            raise ValueError(
                f"Unknown {group} service={service!r}; expected one of {sorted(services)}"
            )
        self._group = group
        self._service = service
        self._path = Path(active_root) / group / f"{service}.log"
        self._state = INACTIVE
        self._stream: TextIO | None = None
        self.createLock()

    @property
    def group(self) -> str:
        return self._group

    @property
    def service(self) -> str:
        return self._service

    @property
    def path(self) -> Path:
        return self._path

    @property
    def state(self) -> str:
        return self._state

    def start_session(self) -> None:
        """Activate and truncate; restartable after end_session."""
        with self.lock:
            if self._state == CLOSED:
                raise RuntimeError("ActiveSessionHandler is closed")
            self._open("w")
            self._state = ACTIVE

    def end_session(self) -> None:
        """Deactivate, detach, and retain the file; no-op unless active."""
        with self.lock:
            if self._state != ACTIVE:
                return
            stream = self._stream
            self._stream = None
            flush_error: BaseException | None = None
            close_error: BaseException | None = None
            try:
                if stream is not None and not stream.closed:
                    stream.flush()
            except BaseException as error:
                flush_error = error
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except BaseException as error:
                close_error = error
            self._state = INACTIVE
            if flush_error is not None:
                if close_error is not None:
                    raise flush_error from close_error
                raise flush_error
            if close_error is not None:
                raise close_error

    def emit(self, record: logging.LogRecord) -> None:
        # handle() already holds self.lock; RLock makes the re-entry safe.
        with self.lock:
            if self._state != ACTIVE:
                return
            try:
                message = self.format(record)
            except Exception:
                self.handleError(record)
                return
            try:
                self._stream.write(message + "\n")  # type: ignore[union-attr]
                self._stream.flush()  # type: ignore[union-attr]
            except Exception:
                try:
                    self._close_stream()
                finally:
                    self._state = INACTIVE
                self.handleError(record)

    def close(self) -> None:
        """Terminal close; idempotent; start/emit refused afterwards."""
        with self.lock:
            super().close()
            self._close_stream()
            self._state = CLOSED

    def _open(self, mode: str) -> None:
        if self._stream is not None and not self._stream.closed:
            if mode == "a":
                return
            self._close_stream()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open(mode, encoding="utf-8")

    def _flush(self) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.flush()

    def _close_stream(self) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        self._stream = None
