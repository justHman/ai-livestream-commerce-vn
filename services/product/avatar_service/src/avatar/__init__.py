"""Canonical self-host avatar service package."""

from avatar.bootstrap.app_factory import create_app
from avatar.engines.base import (
    AvatarEngine,
    EngineError,
    EngineUnavailable,
    FullPipelineBackend,
    RenderBackend,
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from avatar.engines.avatarforcing import AvatarForcingEngine
from avatar.engines.mock import MockRenderBackend
from avatar.sessions import SessionManager

__all__ = [
    "AvatarEngine",
    "AvatarForcingEngine",
    "EngineError",
    "EngineUnavailable",
    "FullPipelineBackend",
    "MockRenderBackend",
    "RenderBackend",
    "SessionManager",
    "StartOptions",
    "StartResult",
    "StreamingAvatarBackend",
    "create_app",
]
test
