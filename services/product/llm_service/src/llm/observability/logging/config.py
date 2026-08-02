"""Pydantic-esque config validation for logging.

Uses stdlib-only validation (no pydantic dependency at runtime).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LoggingConfig:
    """Immutable logging configuration validated at startup."""

    level: str = "INFO"
    service: str = "llm"
    environment: str = "dev"
    log_dir: str = ""
    json_format: bool = False
    retention_days: int = 30
    _validated: bool = field(default=False, repr=False, init=False)

    _VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
    _APPROVED_FIELDS = frozenset(
        {
            "request_id",
            "session_id",
            "user_id",
            "shop_id",
            "trace_id",
            "span_id",
            "service",
            "environment",
            "component",
            "duration_ms",
            "status_code",
            "method",
            "path",
            "topic",
            "event",
            "error",
            "warning",
            "count",
        }
    )
    _SECRET_KEYS = frozenset(
        {
            "password",
            "secret",
            "token",
            "api_key",
            "api-key",
            "access_key",
            "secret_key",
            "authorization",
            "auth",
            "credential",
            "jwt",
            "refresh_token",
        }
    )

    @classmethod
    def from_env(cls) -> "LoggingConfig":
        """Build from environment variables with defaults."""
        return cls(
            level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            service=os.environ.get("SERVICE_NAME", "llm"),
            environment=os.environ.get("APP_ENV", "dev"),
            log_dir=os.environ.get("LOG_DIR", ""),
            json_format=os.environ.get("LOG_JSON", "").lower() in ("1", "true", "yes"),
            retention_days=int(os.environ.get("LOG_RETENTION_DAYS", "30")),
        )

    def validate(self) -> None:
        """Validate config, raising ``ValueError`` on invalid values."""
        if self.level not in self._VALID_LEVELS:
            raise ValueError(
                f"Invalid LOG_LEVEL={self.level!r}; expected one of {sorted(self._VALID_LEVELS)}"
            )
        if not self.service or not self.service.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid SERVICE_NAME={self.service!r}")
        if self.environment not in ("dev", "test", "staging", "production"):
            raise ValueError(f"Invalid APP_ENV={self.environment!r}")
        if self.log_dir:
            p = Path(self.log_dir)
            if not p.is_absolute():
                raise ValueError(f"LOG_DIR must be absolute: {self.log_dir!r}")
        if self.retention_days < 1:
            raise ValueError(f"LOG_RETENTION_DAYS must be >= 1, got {self.retention_days}")

    @staticmethod
    def is_approved_field(key: str) -> bool:
        """Return True if *key* is in the approved-field allowlist."""
        return key in LoggingConfig._APPROVED_FIELDS

    @staticmethod
    def sanitize_extra(extra: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *extra* with secret keys omitted."""
        return {k: v for k, v in extra.items() if k.lower() not in LoggingConfig._SECRET_KEYS}


def validate_config(**overrides: Any) -> LoggingConfig:
    """Build and validate a ``LoggingConfig`` from env + overrides.

    Raises ``ValueError`` on invalid values.
    """
    cfg = LoggingConfig.from_env()
    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    cfg.validate()
    return cfg
