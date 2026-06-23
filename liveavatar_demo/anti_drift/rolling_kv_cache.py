"""Rolling KV Cache with noise level tracking.

Fixed-size FIFO window per denoising timestep. Each entry stores the
KV tensors for one block along with the noise level sigma at which
the block was computed. This enables History Corrupt (adding matching
noise to cached KV) and bounds memory for infinite-length generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import torch


@dataclass
class KVEntry:
    """One cached KV block with its associated noise level."""

    key: torch.Tensor      # (num_heads, block_len, head_dim)
    value: torch.Tensor    # (num_heads, block_len, head_dim)
    sigma: float           # noise level when this KV was produced


class RollingKVCache:
    """Per-timestep rolling KV cache with FIFO eviction.

    Parameters
    ----------
    window_size : int
        Maximum number of past blocks to retain (L in the paper).
    num_heads : int
        Number of attention heads.
    head_dim : int
        Dimension per head.
    block_len : int
        Number of latent frames per block (default 3).
    device : str
        Torch device.
    dtype : torch.dtype
        Tensor dtype.
    """

    def __init__(
        self,
        window_size: int = 4,
        num_heads: int = 30,
        head_dim: int = 128,
        block_len: int = 3,
        device: str = "cpu",
        dtype: torch.dtype = torch.float32,
    ) -> None:
        self.window_size = window_size
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.block_len = block_len
        self.device = device
        self.dtype = dtype

        self._entries: list[KVEntry] = []

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def append(self, key: torch.Tensor, value: torch.Tensor, sigma: float) -> None:
        """Add a new KV entry, evicting the oldest if over window."""
        self._entries.append(
            KVEntry(key=key.detach(), value=value.detach(), sigma=sigma)
        )
        if len(self._entries) > self.window_size:
            self._entries.pop(0)

    def get_all(self) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Return concatenated (keys, values, sigmas) for all cached blocks.

        Returns
        -------
        keys : (N, num_heads, block_len, head_dim)
        values : (N, num_heads, block_len, head_dim)
        sigmas : (N,)
        """
        if not self._entries:
            empty = torch.zeros(
                0, self.num_heads, self.block_len, self.head_dim,
                device=self.device, dtype=self.dtype,
            )
            return empty, empty, torch.zeros(0, device=self.device)
        keys = torch.stack([e.key for e in self._entries])
        values = torch.stack([e.value for e in self._entries])
        sigmas = torch.tensor([e.sigma for e in self._entries], device=self.device)
        return keys, values, sigmas

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def is_full(self) -> bool:
        return len(self._entries) >= self.window_size

    def clear(self) -> None:
        self._entries.clear()

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def state_dict(self) -> dict:
        return {
            "window_size": self.window_size,
            "num_entries": len(self._entries),
            "sigmas": [e.sigma for e in self._entries],
        }

    def __repr__(self) -> str:
        return (
            f"RollingKVCache(window={self.window_size}, "
            f"size={self.size}, full={self.is_full})"
        )
