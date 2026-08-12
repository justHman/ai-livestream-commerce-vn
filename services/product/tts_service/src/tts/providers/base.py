"""TTSProvider contract — the only model-runtime dependency the scheduler sees.

Change T design: the scheduler drives exactly one provider per runtime lane via
this protocol. Protocol (not ABC): providers only need to satisfy the surface;
there is no shared implementation logic to inherit.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Protocol, runtime_checkable

from tts.providers.capabilities import ProviderCapabilities
from tts.providers.models import AudioResult, ProviderRequest, ProviderResult


@runtime_checkable
class TTSProvider(Protocol):
    """Provider-neutral synthesis surface for the scheduler."""

    def capabilities(self) -> ProviderCapabilities: ...

    def batch_key(self, request: ProviderRequest) -> Hashable:
        """Compatibility key: requests sharing a key may share one provider batch."""

    async def synthesize(self, request: ProviderRequest) -> AudioResult:
        """Synthesize one request (used for CPU/non-batch providers too)."""

    async def synthesize_batch(self, requests: Sequence[ProviderRequest]) -> Sequence[ProviderResult]:
        """Synthesize compatible requests in one provider batch, order preserved."""

    def enroll_voice(self, reference_audio: bytes, options: dict) -> object:
        """Enroll a cloned voice from reference audio; returns an opaque profile."""


class ProviderVoiceProfile:
    """Opaque provider-side voice profile handle (payload stays provider-side)."""


class EnrollmentOptions:
    """Provider-neutral enrollment knobs (name, style default, etc.)."""

    def __init__(self, display_name: str = "", style: str = "natural") -> None:
        self.display_name = display_name
        self.style = style
