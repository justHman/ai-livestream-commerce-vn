"""Canonical ASGI entrypoint with a staged legacy compatibility seam."""

from core.server import app, create_app

__all__ = ["app", "create_app"]
