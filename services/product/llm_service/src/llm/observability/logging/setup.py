"""Idempotent service-logger setup with explicit resource cleanup."""

from __future__ import annotations

import logging
import sys
from threading import Lock

from llm.observability.logging.config import LoggingConfig, validate_config
from llm.observability.logging.daily_handler import DailyHandler
from llm.observability.logging.filters import ContextFilter, StructuredFieldsFilter
from llm.observability.logging.formatter import ContextFormatter

_HANDLER_MARKER = "_llm_observability_handler"
_SETUP_LOCK = Lock()


def _owned_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [handler for handler in logger.handlers if getattr(handler, _HANDLER_MARKER, False)]


def setup_logging(config: LoggingConfig | None = None, **overrides: object) -> logging.Logger:
    """Configure one service logger once without mutating unrelated handlers.

    Attaches a TTY console handler (color only when the stream is a terminal)
    and a UTC-dated daily file handler under `{runtime_root}/daily/`, both
    protected by the context and structured-field filters.
    """
    if config is not None and overrides:
        raise ValueError("Pass either config or overrides, not both")
    resolved = config or validate_config(**overrides)
    logger = logging.getLogger(resolved.service)
    with _SETUP_LOCK:
        if _owned_handlers(logger):
            return logger

        console = logging.StreamHandler(sys.stderr)
        setattr(console, _HANDLER_MARKER, True)
        console.setLevel(resolved.level)
        colorize = resolved.color == "auto" and sys.stderr.isatty()
        console.setFormatter(ContextFormatter(service=resolved.service, colorize=colorize))
        console.addFilter(ContextFilter())
        console.addFilter(StructuredFieldsFilter())

        daily = DailyHandler(
            service=resolved.service,
            daily_root=resolved.runtime_root / "daily",
            retention_days=resolved.retention_days,
        )
        setattr(daily, _HANDLER_MARKER, True)
        daily.setLevel(resolved.level)
        daily.setFormatter(ContextFormatter(service=resolved.service, colorize=False))
        daily.addFilter(ContextFilter())
        daily.addFilter(StructuredFieldsFilter())
        daily.retain()

        logger.addHandler(console)
        logger.addHandler(daily)
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
