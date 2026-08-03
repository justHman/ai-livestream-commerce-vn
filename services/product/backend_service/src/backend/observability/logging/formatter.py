"""Aligned human logfmt formatter with TTY-only color.

Task 1.9 owns this module. Records render as:

    DD-MM-YYTHH:mm:ssZ | LEVEL   | service : message evt=started sid=abc

The level column is left-aligned to 7 characters and the service column to 8,
matching the longest service name `postgres`. Field names use the approved
short forms; values containing whitespace are quoted. Color is applied only
when explicitly enabled by a console handler attached to a TTY; file handlers
never colorize.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from backend.observability.logging.config import APPROVED_FIELDS, REDACTION_FIELD

_UTC = timezone.utc
_WHITESPACE = re.compile(r"\s")
_FIELD_NAMES = {
    "session_id": "sid",
    "request_id": "rid",
    "event": "evt",
    "component": "cmp",
}
_LEVEL_COLORS = {
    logging.DEBUG: "36",
    logging.INFO: "32",
    logging.WARNING: "33",
    logging.ERROR: "31",
}


class ContextFormatter(logging.Formatter):
    """Render one aligned logfmt line; color only when explicitly enabled."""

    def __init__(self, *, service: str, colorize: bool = False) -> None:
        super().__init__("%(message)s")
        self._service = service
        self._colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        fields = " ".join(
            _render_field(key, record.__dict__[key])
            for key in sorted(APPROVED_FIELDS | {REDACTION_FIELD})
            if key in record.__dict__
        )
        timestamp = datetime.now(_UTC).strftime("%d-%m-%yT%H:%M:%SZ")
        level = (record.levelname or "INFO").ljust(7)
        line = f"{timestamp} | {level} | {self._service:<8}: {message}"
        if fields:
            line = f"{line} {fields}"
        if self._colorize:
            code = _LEVEL_COLORS.get(record.levelno)
            if code is not None:
                line = f"\x1b[{code}m{line}\x1b[0m"
        return line


def _render_field(key: str, value: object) -> str:
    name = _FIELD_NAMES.get(key, key)
    rendered = str(value)
    if rendered == "" or _WHITESPACE.search(rendered) is not None:
        escaped = rendered.replace("\\", "\\\\").replace('"', '\\"')
        rendered = f'"{escaped}"'
    return f"{name}={rendered}"
