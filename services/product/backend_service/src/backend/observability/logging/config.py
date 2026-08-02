"""Validated stdlib logging configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
SERVICE_NAME = "backend"
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
            raise ValueError("LOG_RETENTION_DAYS must be an integer")
        if self.retention_days < 1:
            raise ValueError("LOG_RETENTION_DAYS must be >= 1")
        if self.color not in {"auto", "never"}:
            raise ValueError("LOG_COLOR must be 'auto' or 'never'")


def validate_config(**overrides: object) -> LoggingConfig:
    """Build and validate backend logging configuration."""
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
    try:
        values["retention_days"] = int(values["retention_days"])
    except (TypeError, ValueError) as exc:
        raise ValueError("LOG_RETENTION_DAYS must be an integer") from exc
    return LoggingConfig(**values)  # type: ignore[arg-type]
