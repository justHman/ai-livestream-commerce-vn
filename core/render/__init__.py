"""core.render — renderer backends behind the RenderBackend seam."""

from .base import RenderBackend, StartOptions, StartResult
from .mock import MockRenderBackend

__all__ = ["RenderBackend", "StartOptions", "StartResult", "MockRenderBackend"]
