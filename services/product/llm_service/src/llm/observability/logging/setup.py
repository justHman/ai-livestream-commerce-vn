"""Idempotent service-logger setup with explicit resource cleanup."""

from __future__ import annotations

import logging
import sys
from threading import Lock

from llm.observability.logging.config import LoggingConfig, validate_config
from llm.observability.logging.filters import ContextFilter, StructuredFieldsFilter
from llm.observability.logging.formatter import ContextFormatter

_HANDLER_MARKER = "_llm_observability_handler"
_SETUP_LOCK = Lock()


def _owned_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [handler for handler in logger.handlers if getattr(handler, _HANDLER_MARKER, False)]


def setup_logging(config: LoggingConfig | None = None, **overrides: object) -> logging.Logger:
    """Configure one service logger once without mutating unrelated handlers."""
    if config is not None and overrides:
        raise ValueError("Pass either config or overrides, not both")
    resolved = config or validate_config(**overrides)
    logger = logging.getLogger(resolved.service)
    with _SETUP_LOCK:
        if _owned_handlers(logger):
            return logger
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_MARKER, True)
        handler.setLevel(resolved.level)
        handler.setFormatter(ContextFormatter(service=resolved.service))
        handler.addFilter(ContextFilter())
        handler.addFilter(StructuredFieldsFilter())
        logger.addHandler(handler)
        logger.setLevel(resolved.level)
        logger.propagate = False
    return logger


def reset_logging(service: str = "llm") -> None:
    """Remove and close only handlers owned by this observability package."""
    logger = logging.getLogger(service)
    with _SETUP_LOCK:
        handlers = _owned_handlers(logger)
        for handler in handlers:
            logger.removeHandler(handler)
        for handler in handlers:
            handler.close()
