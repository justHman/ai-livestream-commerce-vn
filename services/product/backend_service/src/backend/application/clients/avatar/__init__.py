"""Avatar outbound clients (self-host service proxy, LiveAvatar, Baidu Xiling)."""

from backend.application.clients.avatar.self_hosted import SelfHostedAvatarClient
from backend.application.clients.avatar.liveavatar import LiveAvatarClient

__all__ = ["LiveAvatarClient", "SelfHostedAvatarClient"]
