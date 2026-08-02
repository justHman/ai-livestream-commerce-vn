"""Minimal safe formatter; Task 1.9 owns the final logfmt contract."""

from __future__ import annotations

import logging

from avatar.observability.logging.config import APPROVED_FIELDS, REDACTION_FIELD


class ContextFormatter(logging.Formatter):
    """Format a message followed by approved structured fields."""

    def __init__(self, *, service: str) -> None:
        super().__init__("%(levelname)s %(name)s %(message)s")
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = " ".join(
            f"{key}={record.__dict__[key]}"
            for key in sorted(APPROVED_FIELDS | {REDACTION_FIELD})
            if key in record.__dict__
        )
        return f"{base} service={self._service}{' ' + fields if fields else ''}"
