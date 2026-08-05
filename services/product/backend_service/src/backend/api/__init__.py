"""backend.api — HTTP/WS transport package for the backend service.

Contains cross-version middleware, security, shared dependencies, and
exception handlers.  Versioned routes live under ``backend.api.v1`` after
the 1.20 migration.
"""

from . import dependencies, exception_handlers

__all__ = ["dependencies", "exception_handlers"]
