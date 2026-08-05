"""LiveKit publishing for the avatar service."""

# The legacy AudioTrackPublisher lived in avatar/publishing.py (module); the
# self-host layout turned publishing/ into a package. Import its contents here
# so avatar.publishing re-exports both interfaces and the backend publishing seam's
# shim (``from avatar.publishing import *``) keeps the parity contract.
from avatar.publishing.legacy import AudioTrackPublisher, LiveKitPublisherRegistry, publish_enabled  # noqa: F401
from avatar.publishing.livekit import (
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
