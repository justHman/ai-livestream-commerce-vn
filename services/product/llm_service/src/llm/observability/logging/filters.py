"""Logging filters that inject context and sanitise records.

ContextFilter
    Adds ``request_id``, ``session_id``, ``user_id``, ``shop_id``,
    ``trace_id``, ``span_id``, ``service``, ``environment`` from the
    current ContextVar to every log record.
SecretFilter
    Redacts known secret keys from ``extra`` dicts.
"""

from __future__ import annotations

import logging

from llm.observability.context import get_all as get_context
from llm.observability.logging.config import LoggingConfig


class ContextFilter(logging.Filter):
    """Inject observability context fields into every log record."""

    def __init__(self, service: str = "", environment: str = "") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_context()
        for key, val in ctx.items():
            setattr(record, key, val)
        if self._service and not hasattr(record, "service"):
            record.service = self._service
        if self._environment and not hasattr(record, "environment"):
            record.environment = self._environment
        return True


class SecretFilter(logging.Filter):
    """Remove secret keys from record ``extra`` dicts."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key in LoggingConfig._SECRET_KEYS:
                record.__dict__.pop(key, None)
        return True
