"""Minimal ``logging.Formatter`` that includes context fields.

Output format::

    [timestamp] LEVEL     service  message  key=val  key=val
"""

from __future__ import annotations

import logging

from tts.observability.logging.config import LoggingConfig


class ContextFormatter(logging.Formatter):
    """Format records with context fields appended as ``key=val``.

    This is a minimal, not-quite-logfmt implementation.  The full four-level
    alignment and logfmt spec (Task 1.9) is **not** implemented here.
    """

    _BASE_FMT = "%(asctime)s %(levelname)-7s %(message)s"
    _DATEFMT = "%Y-%m-%d %H:%M:%S"

    # Fields that are already rendered in the base format or are internal.
    _SKIP = frozenset(
        {
            "asctime",
            "created",
            "levelname",
            "levelno",
            "message",
            "name",
            "pathname",
            "filename",
            "lineno",
            "funcName",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "threadName",
            "thread",
            "processName",
            "process",
            "msecs",
            "relativeCreated",
            "msg",
            "args",
        }
    )

    def __init__(self, *, service: str = "", environment: str = "") -> None:
        super().__init__(fmt=self._BASE_FMT, datefmt=self._DATEFMT)
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        # Ensure service and environment are on the record.
        if self._service and not hasattr(record, "service"):
            record.service = self._service
        if self._environment and not hasattr(record, "environment"):
            record.environment = self._environment

        extras: list[str] = []
        for key, val in sorted(record.__dict__.items()):
            if key in self._SKIP:
                continue
            if key.startswith("_"):
                continue
            if not LoggingConfig.is_approved_field(key):
                continue
            extras.append(f"{key}={val}")

        # Append extras to the formatted message.
        base = super().format(record)
        if extras:
            base += "  " + "  ".join(extras)
        return base
