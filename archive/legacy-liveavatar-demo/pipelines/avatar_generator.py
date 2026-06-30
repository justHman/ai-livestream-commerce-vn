"""Avatar Generator — Mock DiT denoiser with real anti-drift strategies.

In production, this would load Wan2.2-S2V-14B + LoRA weights and perform
real flow-matching denoising. For this demo, we simulate the DiT forward
pass with audio-conditioned noise to preserve timing structure while
implementing all 4 anti-drift strategies with real tensor operations.

Anti-drift strategies (from LiveAvatar arXiv 2512.04677):
1. History Corrupt — add noise to KV cache at current sigma
2. AAS — replace sink frame with model's own x0 prediction
3. Rolling RoPE — reassign positions within rolling window
4. Rolling KV Cache — FIFO eviction, bounded memory
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn.functional as F

from anti_drift import (
    AdaptiveAttentionSink,
    RollingKVCache,
    RollingRoPE,
    corrupt_kv_cache,
)


@dataclass
class GeneratorConfig:
    """Configuration for the avatar generator."""

    # Latent dimensions (matching Wan2.2 VAE)
    latent_channels: int = 16
    latent_height: int = 50  # 400 / 8
    latent_width: int = 90   # 720 / 8
    block_size: int = 3       # frames per block

    # Denoising
    num_steps: int = 4        # T denoising steps (DMD distilled)
    sigma_min: float = 0.002
    sigma_max: float = 80.0

    # Anti-drift
    kv_window_size: int = 4
    num_heads: int = 30
    head_dim: int = 128
    enable_history_corrupt: bool = True
    enable_aas: bool = True
    enable_rolling_rope: bool = True

    # Audio conditioning
    audio_embed_dim: int = 768


class MockAvatarGenerator:
    """Mock DiT denoiser with full anti-drift implementation.

    Instead of loading the 14B model, this generates plausible latents
    conditioned on audio embeddings. The anti-drift strategies use real
    tensor operations identical to the production implementation.

    Parameters
    ----------
    config : GeneratorConfig
        Generator configuration.
    device : str
        Torch device.
    """

    def __init__(self, config: Optional[GeneratorConfig] = None, device: str = "cpu") -> None:
        self.config = config or GeneratorConfig()
        self.device = device
        self.c = self.config

        # Initialize per-timestep KV caches
        self.kv_caches: list[RollingKVCache] = []
        self._init_kv_caches()

        # AAS and RoPE will be initialized per-stream
        self._aas: Optional[AdaptiveAttentionSink] = None
        self._rope: Optional[RollingRoPE] = None

        # Sigma schedule (geometric spacing for flow matching)
        self.sigmas = torch.linspace(
            self.c.sigma_max, self.c.sigma_min, self.c.num_steps + 1
        )

    def _init_kv_caches(self) -> None:
        """Create per-timestep rolling KV caches."""
        self.kv_caches = [
            RollingKVCache(
                window_size=self.c.kv_window_size,
                num_heads=self.c.num_heads,
                head_dim=self.c.head_dim,
                block_len=self.c.block_size,
                device=self.device,
            )
            for _ in range(self.c.num_steps)
        ]

    def start_stream(self, ref_latent: torch.Tensor) -> None:
        """Initialize a new streaming session.

        Parameters
        ----------
        ref_latent : torch.Tensor
            Latent encoding of the reference image.
            Shape: (C, H, W).
        """
        # Reset KV caches
        self._init_kv_caches()

        # Initialize AAS with reference image latent
        self._aas = AdaptiveAttentionSink(
            ref_latent=ref_latent,
            enabled=self.config.enable_aas,
        )

        # Initialize Rolling RoPE
        self._rope = RollingRoPE(
            window_size=self.c.kv_window_size,
            block_len=self.c.block_size,
            enabled=self.config.enable_rolling_rope,
        )

    def generate_block(
        self,
        audio_embed: torch.Tensor,
        block_idx: int,
    ) -> torch.Tensor:
        """Generate one block of video latents.

        Implements Algorithm 3 from the LiveAvatar paper:
        1. Init noise x ~ N(0, I)
        2. For each denoising step j (high to low sigma):
           a. Corrupt KV cache at current sigma
           b. Compute velocity v from mock DiT
           c. Update x with Euler step: x = x + v * dt
           d. Store KV in rolling cache
        3. After first block: AAS update sink
        4. VAE decode would happen externally

        Parameters
        ----------
        audio_embed : torch.Tensor
            Audio embedding for this block.
            Shape: (num_audio_frames, embed_dim).
        block_idx : int
            Current block index (0-based).

        Returns
        -------
        x0 : torch.Tensor
            Predicted clean latent.
            Shape: (block_size, C, H, W).
        """
        B, C, H, W = (
            self.c.block_size,
            self.c.latent_channels,
            self.c.latent_height,
            self.c.latent_width,
        )

        # 1. Init noise
        x = torch.randn(B, C, H, W, device=self.device) * self.sigmas[0]

        # 2. Denoising loop (T steps, high sigma → low sigma)
        for j in range(self.c.num_steps):
            sigma_j = self.sigmas[j].item()
            sigma_next = self.sigmas[j + 1].item()
            dt = sigma_next - sigma_j

            # 2a. Get corrupted KV from history
            cache = self.kv_caches[j]
            noisy_keys, noisy_values = corrupt_kv_cache(
                cache, current_sigma=sigma_j,
                enabled=self.c.enable_history_corrupt,
            )

            # 2b. Mock DiT forward — audio-conditioned velocity
            v = self._mock_dit_forward(x, sigma_j, audio_embed, noisy_keys, noisy_values)

            # 2c. Euler step
            x = x + v * dt

            # 2d. Store KV in rolling cache
            # Simulate KV extraction from the "attention layers"
            mock_key = torch.randn(
                self.c.num_heads, B, self.c.head_dim,
                device=self.device,
            )
            mock_value = torch.randn(
                self.c.num_heads, B, self.c.head_dim,
                device=self.device,
            )
            cache.append(mock_key, mock_value, sigma_j)

        # Advance Rolling RoPE
        if self._rope is not None:
            self._rope.advance()

        # 3. AAS update after first block
        if block_idx == 0 and self._aas is not None:
            self._aas.update_sink(x)

        return x

    def _mock_dit_forward(
        self,
        x: torch.Tensor,
        sigma: float,
        audio_embed: torch.Tensor,
        cached_keys: torch.Tensor,
        cached_values: torch.Tensor,
    ) -> torch.Tensor:
        """Mock DiT forward pass.

        In production, this would be the full Wan2.2-S2V-14B forward
        with cross-attention to audio/text, self-attention with KV cache,
        and RoPE position encoding.

        Here we simulate the output: a velocity field that pushes
        x toward a clean image conditioned on the audio embedding.
        The audio conditioning modulates the magnitude and direction.

        Parameters
        ----------
        x : torch.Tensor
            Current noisy latent (B, C, H, W).
        sigma : float
            Current noise level.
        audio_embed : torch.Tensor
            Audio embedding (num_frames, embed_dim).
        cached_keys, cached_values : torch.Tensor
            Corrupted KV from cache.

        Returns
        -------
        v : torch.Tensor
            Predicted velocity (B, C, H, W).
        """
        # Audio influence: project audio embed to latent modulation
        # Simple linear projection (real model would use cross-attention)
        C = self.c.latent_channels
        audio_mean = audio_embed.mean(dim=0)  # (embed_dim,)
        audio_scale = torch.sigmoid(audio_mean[:C].to(self.device))  # (C,)
        audio_shift = torch.tanh(audio_mean[C : 2 * C].to(self.device)) * 0.1  # (C,)

        # Velocity = direction toward clean image modulated by audio
        # In flow matching: v = (x0 - x) / sigma for the noise-to-data direction
        # We simulate x0 as a combination of audio-modulated template
        clean_signal = torch.randn_like(x) * 0.1  # mock "clean" signal
        clean_signal = clean_signal * audio_scale.view(1, C, 1, 1)
        clean_signal = clean_signal + audio_shift.view(1, C, 1, 1)

        # Flow-matching velocity: push toward clean signal
        # v = (x0 - x) / sigma approximated
        v = (clean_signal - x) / max(sigma, 0.01)

        # Scale down at lower sigma (convergence regime)
        scale = sigma / self.c.sigma_max
        v = v * scale

        return v

    @property
    def aas(self) -> Optional[AdaptiveAttentionSink]:
        return self._aas

    @property
    def rope(self) -> Optional[RollingRoPE]:
        return self._rope

    def get_cache_sizes(self) -> list[int]:
        """Return current size of each timestep's KV cache."""
        return [c.size for c in self.kv_caches]

    def get_status(self) -> dict:
        """Return generator status for the UI dashboard."""
        return {
            "kv_cache_sizes": self.get_cache_sizes(),
            "aas_enabled": self._aas.enabled if self._aas else False,
            "aas_updated": self._aas.is_updated if self._aas else False,
            "rope_enabled": self._rope.enabled if self._rope else False,
            "rope_block": self._rope.current_block if self._rope else 0,
            "history_corrupt_enabled": self.config.enable_history_corrupt,
        }
