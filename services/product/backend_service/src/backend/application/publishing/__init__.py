"""LiveKit publishing for the avatar service."""

# The legacy AudioTrackPublisher lived in avatar/publishing.py (module); the
# self-host layout turned publishing/ into a package. Import its contents here
# so avatar.publishing re-exports both interfaces and core.livekit_publish's
# shim (``from backend.application.publishing import *``) keeps the parity contract.
from backend.application.publishing.legacy import (
    AudioTrackPublisher,
    LiveKitPublisherRegistry,
    publish_enabled,  # noqa: F401
)
from backend.application.publishing.livekit import (
    LiveKitConfigError,
    LiveKitPublisher,
    mint_room_token,
)

__all__ = [
    "AudioTrackPublisher",
    "LiveKitConfigError",
    "LiveKitPublisher",
    "LiveKitPublisherRegistry",
    "mint_room_token",
    "publish_enabled",
]
