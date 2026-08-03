"""Validated stdlib logging configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
MAX_RETENTION_DAYS = 3650
SERVICE_NAME = "llm"
APPROVED_FIELDS = frozenset(
    {
        "session_id",
        "request_id",
        "trace_id",
        "component",
        "event",
        "method",
        "path",
        "status_code",
        "latency_ms",
        "provider",
        "error",
    }
)
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|auth(?:orization)?|cookie|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)
REDACTION_FIELD = "redacted"
REDACTION_MARKER = "[REDACTED]"
OMITTED_FIELDS = frozenset(
    {
        "prompt",
        "viewer_message",
        "viewer_messages",
        "shop_profile",
        "provider_body",
        "request_body",
        "response_body",
        "customer_payload",
    }
)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging values validated before handler creation."""

    level: str = "INFO"
    service: str = SERVICE_NAME
    runtime_root: Path = Path(".runtime/logs")
    retention_days: int = 30
    color: str = "auto"

    def __post_init__(self) -> None:
        if self.level not in VALID_LEVELS:
            raise ValueError(
                f"Invalid LOG_LEVEL={self.level!r}; expected one of {sorted(VALID_LEVELS)}"
            )
        if self.service != SERVICE_NAME:
            raise ValueError(f"Invalid SERVICE_NAME={self.service!r}; expected {SERVICE_NAME!r}")
        if not isinstance(self.runtime_root, (str, Path)) or not str(self.runtime_root).strip():
            raise ValueError("LOG_ROOT must not be empty")
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))
        if not isinstance(self.retention_days, int) or isinstance(self.retention_days, bool):
            raise ValueError("LOG_RETENTION_DAYS must be a bounded integer")
        if not 1 <= self.retention_days <= MAX_RETENTION_DAYS:
            raise ValueError(
                f"LOG_RETENTION_DAYS must be between 1 and {MAX_RETENTION_DAYS}"
            )
        if self.color not in {"auto", "never"}:
            raise ValueError("LOG_COLOR must be 'auto' or 'never'")


def _parse_retention_days(value: object, *, from_env: bool) -> int:
    if isinstance(value, bool):
        raise ValueError("LOG_RETENTION_DAYS must be an integer")
    if isinstance(value, int):
        return value
    if from_env and isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("LOG_RETENTION_DAYS must be an integer")


def validate_config(**overrides: object) -> LoggingConfig:
    """Build and validate llm logging configuration."""
    values: dict[str, object] = {
        "level": os.getenv("LOG_LEVEL", "INFO"),
        "service": SERVICE_NAME,
        "runtime_root": Path(os.getenv("LOG_ROOT", ".runtime/logs")),
        "retention_days": os.getenv("LOG_RETENTION_DAYS", "30"),
        "color": os.getenv("LOG_COLOR", "auto").lower(),
    }
    unknown = set(overrides) - set(values)
    if unknown:
        raise ValueError(f"Unknown logging configuration: {sorted(unknown)}")
    values.update(overrides)
    values["retention_days"] = _parse_retention_days(
        values["retention_days"], from_env="retention_days" not in overrides
    )
    return LoggingConfig(**values)  # type: ignore[arg-type]
