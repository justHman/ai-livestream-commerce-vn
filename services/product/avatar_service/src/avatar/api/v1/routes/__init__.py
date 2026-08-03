"""Versioned v1 routes for the avatar service."""

from avatar.api.v1.routes.avatars import router as avatars
from avatar.api.v1.routes.sessions import router as sessions

__all__ = ["avatars", "sessions"]