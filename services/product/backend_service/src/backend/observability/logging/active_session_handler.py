"""Active-session log file seam: one path per service, truncate/append/retain.

Task 1.7 owns this module: a logging.Handler writing to a single
`{group}/<service>.log` under an active-sessions root. Session ids never appear
in paths, and service/group names come from fixed allowlists.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

_PRODUCT_SERVICES = frozenset({"backend", "llm", "tts", "avatar"})
_PLATFORM_SERVICES = frozenset({"livekit", "lmcache", "postgres", "redis"})
_GROUPS = {"product": _PRODUCT_SERVICES, "platform": _PLATFORM_SERVICES}


class ActiveSessionHandler(logging.Handler):
    """Write active-session records to `{active_root}/{group}/<service>.log`.

    - start_session truncates the file ahead of the first record of a new session.
    - emit appends within a session.
    - end_session flushes and closes the stream, retaining the file for reads.
    - service/group are allowlist-validated; no untrusted value reaches a path.
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
        self._stream: object = None
        self._lock = threading.RLock()

    @property
    def group(self) -> str:
        return self._group

    @property
    def service(self) -> str:
        return self._service

    @property
    def path(self) -> Path:
        return self._path

    def start_session(self) -> None:
        """Truncate the file ahead of the first record of a new session."""
        with self._lock:
            self._open("w")
            self._flush()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            try:
                self._open("a")
                self._stream.write(message + "\n")  # type: ignore[union-attr]
                self._flush()
            except Exception:
                self.handleError(record)

    def end_session(self) -> None:
        """Flush and close the stream; retain the file for later reads."""
        with self._lock:
            self._flush()
            self._close_stream()

    def close(self) -> None:
        with self._lock:
            super().close()
            self._close_stream()

    def _open(self, mode: str) -> None:
        if self._stream is not None and not self._stream.closed:  # type: ignore[union-attr]
            if mode == "a":
                return
            self._close_stream()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open(mode, encoding="utf-8")

    def _flush(self) -> None:
        if self._stream is not None and not self._stream.closed:  # type: ignore[union-attr]
            self._stream.flush()  # type: ignore[union-attr]

    def _close_stream(self) -> None:
        if self._stream is not None and not self._stream.closed:  # type: ignore[union-attr]
            self._stream.close()  # type: ignore[union-attr]
        self._stream = None
