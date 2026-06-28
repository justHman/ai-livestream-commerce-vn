"""History Corrupt — add Gaussian noise to KV cache at matching timestep noise level.

LiveAvatar §3.3: When the denoiser processes block B_t at timestep j (noise
level sigma_j), the KV cache from previous blocks should also be corrupted
with noise at sigma_j. This prevents the model from "memorising" past frames
too sharply, which would cause drift as the conditioning distribution shifts
over infinite generation.

Implementation: for each cached KV entry (key, value) in the rolling window,
add Gaussian noise proportional to sigma_j so that the noise level matches
the current denoising step.
"""

from __future__ import annotations

import torch

from .rolling_kv_cache import RollingKVCache


def corrupt_kv_cache(
    cache: RollingKVCache,
    current_sigma: float,
    enabled: bool = True,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Return corrupted (keys, values) from the rolling KV cache.

    Parameters
    ----------
    cache : RollingKVCache
        The rolling KV cache for this denoising timestep.
    current_sigma : float
        Noise level of the current denoising step.
    enabled : bool
        If False, return clean KV (no corruption).

    Returns
    -------
    keys : (N, num_heads, block_len, head_dim)
    values : (N, num_heads, block_len, head_dim)
    """
    keys, values, _ = cache.get_all()

    if not enabled or keys.numel() == 0:
        return keys, values

    # Scale noise by current sigma — higher sigma = more corruption
    # This matches the flow-matching intuition: at high noise levels,
    # past information should be less precise.
    noise_scale = current_sigma * 0.1  # attenuated to avoid destroying signal

    noisy_keys = keys + torch.randn_like(keys) * noise_scale
    noisy_values = values + torch.randn_like(values) * noise_scale

    return noisy_keys, noisy_values
