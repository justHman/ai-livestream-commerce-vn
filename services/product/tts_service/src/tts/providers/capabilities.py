"""Typed provider capabilities (Change T task 2.2).

Provider-neutral facts the scheduler and /v1/audio/capabilities expose. Never
carries provider tensors, speaker embeddings, or reference codes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_name: str
    model_revision: str
    sample_rate_hz: int
    supports_native_batch: bool = False
    max_batch_size: int = 1
    supports_voice_cloning: bool = False
    supports_mixed_voice_batch: bool = False
    supported_styles: tuple[str, ...] = ("natural",)
    supported_expressive_cues: tuple[str, ...] = ()
    supported_response_formats: tuple[str, ...] = ("pcm", "wav")

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be > 0")
        if self.max_batch_size < 1:
            raise ValueError("max_batch_size must be >= 1")
        if self.supports_native_batch and not self.supported_styles:
            raise ValueError("a native-batch provider must declare at least one style")
