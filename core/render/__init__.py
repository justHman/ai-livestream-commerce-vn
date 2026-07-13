"""core.render — renderer backends behind the RenderBackend seam."""

from .base import (
    FullPipelineBackend,
    RenderBackend,
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from .mock import MockRenderBackend
from .remote_avatar import RemoteAvatarBackend

__all__ = [
    "FullPipelineBackend",
    "RenderBackend",
    "StartOptions",
    "StartResult",
    "StreamingAvatarBackend",
    "MockRenderBackend",
    "RemoteAvatarBackend",
]
