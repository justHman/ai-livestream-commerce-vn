"""Product-side platform log collector for Task 1.7.

Owns ActiveSessionHandler instances for platform services and exposes
normalized event logging. Lives in the product observability package (not
platform source) and uses no shared runtime package; platform services need
no log-writing code of their own.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from llm.observability.logging.active_session_handler import ActiveSessionHandler


class PlatformCollector:
    """Own platform handlers and log normalized events to platform files."""

    def __init__(self, active_root: Path | str = ".runtime/logs/active-sessions") -> None:
        self._active_root = Path(active_root)
        self._handlers: dict[str, ActiveSessionHandler] = {}
        self._lock = threading.Lock()

    @property
    def active_root(self) -> Path:
        return self._active_root

    def emit_event(self, service: str, message: str, *, level: int = logging.INFO) -> None:
        """Write one normalized event to `{active_root}/platform/{service}.log`."""
        handler = self._handler_for(service)
        record = logging.LogRecord(
            name="platform",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.handle(record)

    def close(self) -> None:
        """Close every owned platform handler (idempotent)."""
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
        for handler in handlers:
            handler.close()

    def _handler_for(self, service: str) -> ActiveSessionHandler:
        with self._lock:
            handler = self._handlers.get(service)
            if handler is not None:
                return handler
            handler = ActiveSessionHandler(
                service=service, group="platform", active_root=self._active_root
            )
            handler.start_session()
            self._handlers[service] = handler
            return handler
