"""ContextVar-based request context with bind/reset and a context manager.

Thread-safe, asyncio-safe.  Uses a sentinel object so ``get()`` never raises
``LookupError`` (Python 3.11 compatible).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# Internal storage
# ---------------------------------------------------------------------------

_fields: tuple[str, ...] = (
    "request_id",
    "session_id",
    "user_id",
    "shop_id",
    "trace_id",
    "span_id",
    "service",
    "environment",
)

_SENTINEL: dict[str, Any] = {}  # unique sentinel — not the same object as {}
_var = contextvars.ContextVar["dict[str, Any]"]("observability_context")
_lock = threading.Lock()


def _ensure() -> dict[str, Any]:
    """Return the mutable dict for the current context, creating one if needed."""
    try:
        d = _var.get()
    except LookupError:
        d = _SENTINEL
    if d is _SENTINEL:
        d = {}
        _var.set(d)
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bind(**kwargs: Any) -> None:
    """Bind key/value pairs into the current context.

    Only keys in ``_fields`` are accepted.
    All values are coerced to ``str``.
    """
    for k, v in kwargs.items():
        if k not in _fields:
            raise ValueError(f"Unknown context field: {k!r}")
    d = _ensure()
    d.update({k: str(v) for k, v in kwargs.items()})


def reset(*keys: str) -> None:
    """Remove *keys* from the current context.

    If no keys are given the entire context is cleared.
    """
    try:
        d = _var.get()
    except LookupError:
        return
    if d is _SENTINEL:
        return
    if keys:
        for k in keys:
            d.pop(k, None)
    else:
        d.clear()


def get(key: str, default: Any = None) -> Any:
    """Return the value for *key* or *default*."""
    try:
        d = _var.get()
    except LookupError:
        return default
    if d is _SENTINEL:
        return default
    return d.get(key, default)


def get_all() -> dict[str, Any]:
    """Return a shallow copy of the current context."""
    try:
        d = _var.get()
    except LookupError:
        return {}
    if d is _SENTINEL:
        return {}
    return dict(d)


context = _var  # the raw ContextVar (useful for framework integration)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def scoped(**kwargs: Any) -> Generator[None, None, None]:
    """Temporarily bind *kwargs* and restore the previous context on exit.

    Usage::

        with observability.scoped(request_id="abc"):
            log.info("inside")
    """
    current = get_all()
    current.update({k: str(v) for k, v in kwargs.items() if k in _fields})
    token = _var.set(current)
    try:
        yield
    finally:
        _var.reset(token)


# ---------------------------------------------------------------------------
# Inbound / outbound metadata helpers
# ---------------------------------------------------------------------------

# fmt: off
INBOUND_HEADERS = {
    "x-request-id": "request_id",
    "x-trace-id": "trace_id",
    "x-span-id": "span_id",
    "x-session-id": "session_id",
    "x-user-id": "user_id",
}
"""Mapping from inbound HTTP header names to context keys."""

OUTBOUND_HEADERS = {v: k for k, v in INBOUND_HEADERS.items()}
"""Reverse mapping from context keys to outbound header names."""


def extract_from_headers(headers: dict[str, str]) -> dict[str, str]:
    """Extract context fields from a dict of inbound headers."""
    result: dict[str, str] = {}
    for header, field in INBOUND_HEADERS.items():
        if header in headers:
            result[field] = headers[header]
    return result


def outbound_headers() -> dict[str, str]:
    """Build outbound header dict from the current context."""
    ctx = get_all()
    return {OUTBOUND_HEADERS[f]: ctx[f] for f in OUTBOUND_HEADERS if f in ctx}
