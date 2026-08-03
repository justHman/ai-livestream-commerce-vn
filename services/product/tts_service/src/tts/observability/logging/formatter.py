"""Aligned human logfmt formatter with TTY-only color and controlled payloads.

Task 1.9 owns this module. Records render as:

    DD-MM-YYTHH:mm:ssZ | LEVEL   | service : message evt=started sid=abc

The level column is left-aligned to 7 characters and the service column to 8,
matching the longest service name `postgres`. Field names use the approved
short forms; values containing whitespace are quoted, and quotes, backslashes
and equals signs are escaped deterministically. Color is applied only when
explicitly enabled by a console handler attached to a TTY; file handlers never
colorize.

Only the leveled debug/info/warning/error record is emitted. A record whose
level is outside that set is rejected by the handler, and the raw exception
traceback is never rendered — a compact ``error=<code>`` field carries the
failure instead. The message itself is bounded and control-character free;
anything else is replaced by ``redacted=[REDACTED]`` and dropped.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from tts.observability.logging.config import (
    APPROVED_FIELDS,
    REDACTION_FIELD,
    REDACTION_MARKER,
)

_UTC = timezone.utc
_WHITESPACE = re.compile(r"\s")
_MAX_MESSAGE_LENGTH = 512
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
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
        if record.levelno not in _LEVEL_COLORS:
            return _rejected(record.levelname, self._service)
        safe_message = _safe_message(record)
        fields = " ".join(
            _render_field(key, record.__dict__[key])
            for key in sorted(APPROVED_FIELDS | {REDACTION_FIELD})
            if key in record.__dict__
        )
        timestamp = datetime.now(_UTC).strftime("%d-%m-%yT%H:%M:%SZ")
        level = (record.levelname or "INFO").ljust(7)
        line = f"{timestamp} | {level} | {self._service:<8}: {safe_message}"
        if fields:
            line = f"{line} {fields}"
        if self._colorize:
            code = _LEVEL_COLORS.get(record.levelno)
            if code is not None:
                line = f"\x1b[{code}m{line}\x1b[0m"
        return line


def _safe_message(record: logging.LogRecord) -> str:
    """Take record.getMessage() only, never the exception renderer.

    The stdlib __str__ path joins exc_info/location records into the output;
    this path formats only the bounded message text so secrets, prompts,
    customer payloads and raw provider bodies never survive in a traceback.
    """
    try:
        message = record.getMessage()
    except Exception:
        return REDACTION_MARKER
    if (
        not isinstance(message, str)
        or len(message) > _MAX_MESSAGE_LENGTH
        or _CONTROL_CHARACTER_PATTERN.search(message) is not None
        or message.strip() == ""
    ):
        return REDACTION_MARKER
    return message


def _rejected(level_name: str | None, service: str) -> str:
    timestamp = datetime.now(_UTC).strftime("%d-%m-%yT%H:%M:%SZ")
    level = (level_name or "INFO").ljust(7)
    return f"{timestamp} | {level} | {service:<8}: unsupported log level"


def _render_field(key: str, value: object) -> str:
    name = _FIELD_NAMES.get(key, key)
    if value == REDACTION_MARKER and key == REDACTION_FIELD:
        return f"{name}={REDACTION_MARKER}"
    rendered = str(value)
    if (
        rendered == ""
        or _WHITESPACE.search(rendered) is not None
        or rendered in ('"', "\\", "=")
        or '"' in rendered
        or "\\" in rendered
        or "=" in rendered
    ):
        escaped = rendered.replace("\\", "\\\\").replace('"', '\\"')
        rendered = f'"{escaped}"'
    return f"{name}={rendered}"