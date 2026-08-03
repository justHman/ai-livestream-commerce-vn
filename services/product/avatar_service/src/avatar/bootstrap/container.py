"""Typed resource container for the avatar service.

Avatar owns an engine, a session registry, and a LiveKit publisher registry
with independent lifecycles, so a small typed container is justified
(unlike LLM/TTS, which each own exactly one active heavyweight engine).
"""

from __future__ import annotations

from avatar.config import EngineConfig, PublishingConfig
from avatar.engines.avatarforcing import AvatarForcingEngine
from avatar.publishing.livekit import LiveKitPublisher
from avatar.sessions import SessionManager


class AvatarContainer:
    """Holds constructed engine, session manager, and publisher."""

    def __init__(
        self,
        engine_cfg: EngineConfig,
        publishing_cfg: PublishingConfig,
    ) -> None:
        self.engine = _build_engine(engine_cfg)
        self.publisher = LiveKitPublisher(publishing_cfg)
        self.sessions = SessionManager(self.engine, self.publisher)

    def close(self) -> None:
        self.sessions.cleanup()


def _build_engine(cfg: EngineConfig):
    """Build the active self-host engine; raises when misconfigured."""
    if cfg.engine == "none":
        return AvatarForcingEngine(model="mock")
    return AvatarForcingEngine.from_config(cfg.to_cfg_dict())
