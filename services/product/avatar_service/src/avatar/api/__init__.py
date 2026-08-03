"""Avatar service API package."""

from avatar.api import (
    dependencies,
    exception_handlers,
    health,
    middleware,
    security,
)

__all__ = ["dependencies", "exception_handlers", "health", "middleware", "security"]