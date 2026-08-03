"""TTS service bootstrap — app factory and bounded lifespan."""

from tts.bootstrap.app_factory import create_app
from tts.bootstrap.lifespan import create_lifespan

__all__ = ["create_app", "create_lifespan"]
