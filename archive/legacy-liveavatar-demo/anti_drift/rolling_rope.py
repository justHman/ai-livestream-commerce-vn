"""Rolling RoPE — dynamically reassign sink frame's position embedding each block.

LiveAvatar §3.3 (Rolling RoPE): In standard RoPE, position indices are fixed
(0, 1, 2, ...). For infinite streaming, this causes position IDs to grow
unboundedly, leading to extrapolation beyond the training range. Rolling RoPE
reassigns the sink frame's position to the most recent block's position range,
so the model always sees a local window of positions.

Implementation:
- For each new block B_t, the sink frame gets position = start_of_B_t.
- Past cached blocks get positions relative to the current block.
- This keeps all position IDs within [0, window_size * block_len).
"""

from __future__ import annotations

import torch


class RollingRoPE:
    """Manages position index assignment for rolling RoPE in streaming mode.

    Parameters
    ----------
    window_size : int
        Number of past blocks in the KV cache window (L).
    block_len : int
        Number of latent frames per block.
    enabled : bool
        Whether Rolling RoPE is active.
    """

    def __init__(
        self,
        window_size: int = 4,
        block_len: int = 3,
        enabled: bool = True,
    ) -> None:
        self.window_size = window_size
        self.block_len = block_len
        self.enabled = enabled
        self._block_idx = 0

    def get_positions(self, cache_size: int) -> torch.Tensor:
        """Return position indices for the current block + cached history.

        In rolling mode, the positions are assigned relative to the current
        block so that they always stay within a bounded range.

        Parameters
        ----------
        cache_size : int
            Number of blocks currently in the KV cache.

        Returns
        -------
        positions : (1, total_len)
            Position indices for RoPE computation.  total_len =
            (cache_size + 1) * block_len  (cached + current block).
        """
        total_blocks = cache_size + 1
        total_len = total_blocks * self.block_len

        if not self.enabled:
            # Non-rolling: absolute positions that grow unboundedly
            offset = self._block_idx * self.block_len
            positions = torch.arange(offset, offset + total_len, dtype=torch.long)
            return positions.unsqueeze(0)

        # Rolling: positions are always in [0, total_len)
        # Sink frame (if using AAS) gets position 0 of current block
        positions = torch.arange(total_len, dtype=torch.long)
        return positions.unsqueeze(0)

    def advance(self) -> None:
        """Move to the next block."""
        self._block_idx += 1

    def reset(self) -> None:
        """Reset position counter."""
        self._block_idx = 0

    @property
    def current_block(self) -> int:
        return self._block_idx

    def __repr__(self) -> str:
        state = "ON" if self.enabled else "OFF"
        return (
            f"RollingRoPE({state}, block={self._block_idx}, "
            f"window={self.window_size}, block_len={self.block_len})"
        )
