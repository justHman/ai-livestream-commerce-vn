"""Provider-neutral TTS provider abstractions (Change T cluster 1)."""

from tts.providers.base import TTSProvider
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.errors import (
    CancelledError,
    CapabilityError,
    DeadlineExceededError,
    OverloadError,
    ProfileNotFoundError,
    ProfileUnauthorizedError,
    ProviderError,
    ProviderInferenceError,
    ProviderUnavailableError,
)
from tts.providers.models import (
    AudioResult,
    GenerationConfig,
    Priority,
    ProviderRequest,
    ProviderResult,
    SynthesisRequest,
)

__all__ = [
    "AudioResult",
    "CancelledError",
    "CapabilityError",
    "DeadlineExceededError",
    "GenerationConfig",
    "OverloadError",
    "Priority",
    "ProfileNotFoundError",
    "ProfileUnauthorizedError",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderInferenceError",
    "ProviderRequest",
    "ProviderResult",
    "ProviderUnavailableError",
    "SynthesisRequest",
    "TTSProvider",
]
