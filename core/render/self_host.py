"""SelfHostRenderBackend — future self-host diffusion renderer (STUB).

When the avatar video model is chosen (research agent in progress — Wan2.2-S2V /
EchoMimic / OmniHuman class, multi-image anti-drift), this backend will:
  - load the diffusion model on the GPU (1-2x A100/4090)
  - run the coarse-grained batch-streaming producer-consumer loop
    (gen batch N+1 while streaming batch N), with first-frame anchor +
    last-frame continuity for anti-drift
  - expose the SAME RenderBackend contract so core/api stays unchanged

Selected via RENDER_BACKEND=self_host. Until implemented, every method raises
NotImplementedError with a clear message — this is intentional and proves the
seam: the API can route to it without any other code change.
"""

from __future__ import annotations

from .base import RenderBackend, StartOptions, StartResult

_MSG = (
    "Self-host renderer not implemented yet. The avatar video model is still "
    "being selected (multi-image anti-drift, batch-streaming on 1-2 GPUs). "
    "Use RENDER_BACKEND=cloud for now."
)


class SelfHostRenderBackend(RenderBackend):
    """Placeholder for the future diffusion renderer."""

    name = "self_host"

    def start(self, opts: StartOptions) -> StartResult:
        raise NotImplementedError(_MSG)

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        raise NotImplementedError(_MSG)

    def interrupt(self, session_id: str) -> None:
        raise NotImplementedError(_MSG)

    def stop(self, session_id: str) -> None:
        raise NotImplementedError(_MSG)
