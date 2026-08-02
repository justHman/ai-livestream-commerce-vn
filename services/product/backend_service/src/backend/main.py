"""Compatibility ASGI entrypoint for the canonical backend package."""

from core.server import app, create_app

__all__ = ["app", "create_app"]
