"""Adaptive Attention Sink (AAS) — replace sink frame latent with model's own output.

LiveAvatar §3.3 (AAS): In standard streaming generation, the "sink" (first)
frame is always the reference image latent. Over long generation, the model
drifts because the attention sink becomes stale. AAS replaces the sink with
the model's own predicted x0 from the first generated block, creating a
self-conditioning loop that stabilises long-range generation.

Implementation:
- Block 0: sink = ref_image_latent (from VAE encode of reference image).
- After block 0 is fully denoised: sink = x_0.detach() (model's own prediction).
- Subsequent blocks: sink latent is replaced in the position embedding table.
"""

from __future__ import annotations

import torch


class AdaptiveAttentionSink:
    """Manages the adaptive attention sink frame.

    Parameters
    ----------
    ref_latent : torch.Tensor
        Initial sink latent from encoding the reference image.
        Shape: (C, H, W) — single frame latent.
    enabled : bool
        Whether AAS is active.
    """

    def __init__(self, ref_latent: torch.Tensor, enabled: bool = True) -> None:
        self._sink = ref_latent.detach().clone()
        self._ref_latent = ref_latent.detach().clone()
        self.enabled = enabled
        self._updated = False

    @property
    def sink(self) -> torch.Tensor:
        """Current sink latent (reference or model-predicted)."""
        return self._sink

    @property
    def is_updated(self) -> bool:
        """True after the sink has been replaced with model output."""
        return self._updated

    def update_sink(self, predicted_x0: torch.Tensor) -> None:
        """Replace the sink with the model's predicted x0 from the first block.

        Parameters
        ----------
        predicted_x0 : torch.Tensor
            Fully denoised latent from block 0.
            Shape: (block_size, C, H, W).
        """
        if not self.enabled:
            return
        # Take the first frame of the predicted block as the new sink
        self._sink = predicted_x0[0].detach().clone()
        self._updated = True

    def reset(self) -> None:
        """Reset sink back to the original reference latent."""
        self._sink = self._ref_latent.detach().clone()
        self._updated = False

    def get_sink_expanded(self, block_size: int) -> torch.Tensor:
        """Return sink latent expanded to match block dimensions for attention.

        Parameters
        ----------
        block_size : int
            Number of latent frames per block.

        Returns
        -------
        sink_expanded : (block_size, C, H, W)
        """
        return self._sink.unsqueeze(0).expand(block_size, -1, -1, -1)

    def __repr__(self) -> str:
        source = "model_output" if self._updated else "reference"
        state = "ON" if self.enabled else "OFF"
        return f"AAS({state}, source={source})"
