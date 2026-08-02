"""Idempotent logging setup.

Call ``setup_logging()`` once at startup to configure the root logger.
Subsequent calls are no-ops.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from backend.observability.logging.config import LoggingConfig, validate_config
from backend.observability.logging.daily_handler import DailyHandler
from backend.observability.logging.active_session_handler import ActiveSessionHandler
from backend.observability.logging.filters import ContextFilter, SecretFilter
from backend.observability.logging.formatter import ContextFormatter

_initialized: bool = False


def setup_logging(config: LoggingConfig | None = None, **overrides: Any) -> None:
    """Configure the root logger once.

    Idempotent — only the first call has an effect.  If handler creation
    fails midway, already-created handlers are closed and removed so no
    stream is leaked.

    Parameters
    ----------
    config
        Pre-validated ``LoggingConfig``.  If ``None``, built from env + overrides.
    **overrides
        Override specific config keys (e.g. ``level="DEBUG"``).
    """
    global _initialized
    if _initialized:
        return

    if config is None:
        config = validate_config(**overrides)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level, logging.INFO))
    root.handlers.clear()

    fmt = ContextFormatter(service=config.service, environment=config.environment)
    created: list[logging.Handler] = []

    try:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, config.level, logging.INFO))
        console.setFormatter(fmt)
        console.addFilter(ContextFilter(service=config.service, environment=config.environment))
        console.addFilter(SecretFilter())
        created.append(console)

        if config.log_dir:
            daily = DailyHandler(config.log_dir, config.service, config.retention_days)
            daily.setLevel(getattr(logging, config.level, logging.INFO))
            daily.setFormatter(fmt)
            daily.addFilter(SecretFilter())
            created.append(daily)

            active = ActiveSessionHandler(config.log_dir, config.service)
            active.setLevel(getattr(logging, config.level, logging.INFO))
            active.setFormatter(fmt)
            active.addFilter(SecretFilter())
            created.append(active)
    except Exception:
        for h in created:
            h.close()
        raise

    for h in created:
        root.addHandler(h)
    _initialized = True


def reset_logging() -> None:
    """Close and remove all root handlers, then reset the init flag.

    Closes file streams so temporary log directories can be removed on
    platforms that lock open files (Windows).
    """
    global _initialized
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    _initialized = False
