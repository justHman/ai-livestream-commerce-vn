"""Canonical self-host avatar service package."""

from avatar.bootstrap.app_factory import create_app
from avatar.engines.base import (
    AvatarEngine,
    EngineError,
    EngineUnavailable,
    StartOptions,
    StartResult,
)
from avatar.engines.avatarforcing import AvatarForcingEngine
from avatar.sessions import SessionManager

__all__ = [
    "AvatarEngine",
    "AvatarForcingEngine",
    "EngineError",
    "EngineUnavailable",
    "SessionManager",
    "StartOptions",
    "StartResult",
    "create_app",
]