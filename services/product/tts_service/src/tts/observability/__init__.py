"""Observability: request context, structured logging, and transport metadata.

Public API
----------
bind, reset, scoped, get, get_all
    ContextVar-based request/session/user context.
extract_from_headers, outbound_headers
    Inbound/outbound transport metadata propagation.
setup_logging, reset_logging, validate_config
    Idempotent logging configuration and validation.

Note: the ``context`` submodule is intentionally NOT re-exported here so the
raw ContextVar never shadows the module name.
"""

from __future__ import annotations

from tts.observability.context import (
    bind,
    extract_from_headers,
    get,
    get_all,
    outbound_headers,
    reset,
    scoped,
)
from tts.observability.logging.config import LoggingConfig, validate_config
from tts.observability.logging.setup import reset_logging, setup_logging

__all__ = [
    "bind",
    "extract_from_headers",
    "get",
    "get_all",
    "outbound_headers",
    "reset",
    "scoped",
    "LoggingConfig",
    "validate_config",
    "setup_logging",
    "reset_logging",
]
