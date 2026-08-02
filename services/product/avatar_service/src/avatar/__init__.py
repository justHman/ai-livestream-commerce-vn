"""Canonical self-host avatar service package."""

from .engines.base import (
    FullPipelineBackend,
    RenderBackend,
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from .engines.mock import MockRenderBackend

__all__ = [
    "FullPipelineBackend",
    "RenderBackend",
    "StartOptions",
    "StartResult",
    "StreamingAvatarBackend",
    "MockRenderBackend",
]
