"""Allowlist-based structured logging filters."""

from __future__ import annotations

import logging
import math
import re

from backend.observability.context import CONTEXT_FIELDS, get_all
from backend.observability.logging.config import APPROVED_FIELDS, OMITTED_FIELDS

REDACTED = "[REDACTED]"
_STANDARD_FIELDS = frozenset(logging.LogRecord("", logging.INFO, "", 0, "", (), None).__dict__)
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def _normalized_key(key: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")


def _is_sensitive_key(key: str) -> bool:
    parts = set(_normalized_key(key).split("_"))
    if parts & {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "password",
        "secret",
        "token",
    }:
        return True
    return "key" in parts and bool(parts & {"access", "api", "secret"})


def _is_safe_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return (
        isinstance(value, str)
        and len(value) <= 512
        and _CONTROL_CHARACTER_PATTERN.search(value) is None
    )


class StructuredFieldsFilter(logging.Filter):
    """Keep approved scalar fields, redact secrets, and omit free-form data."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in tuple(record.__dict__):
            if key in _STANDARD_FIELDS or key.startswith("_"):
                continue
            normalized = _normalized_key(key)
            if _is_sensitive_key(key):
                record.__dict__[key] = REDACTED
            elif normalized in OMITTED_FIELDS or key not in APPROVED_FIELDS:
                record.__dict__.pop(key, None)
            elif not _is_safe_value(record.__dict__[key]):
                record.__dict__.pop(key, None)
        return True


class ContextFilter(logging.Filter):
    """Replace caller-supplied context fields with validated bound values."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_all()
        for field in CONTEXT_FIELDS:
            record.__dict__.pop(field, None)
        record.__dict__.update(context)
        return True
