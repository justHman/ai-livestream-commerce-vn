"""core.render — renderer backends behind the RenderBackend seam."""

from .base import (
    FullPipelineBackend,
    RenderBackend,
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from .mock import MockRenderBackend

__all__ = [
    "FullPipelineBackend",
    "RenderBackend",
    "StartOptions",
    "StartResult",
    "StreamingAvatarBackend",
    "MockRenderBackend",
]
