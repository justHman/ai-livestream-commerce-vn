"""Avatar service bootstrap — app factory, container, and bounded lifespan."""

from avatar.bootstrap.app_factory import create_app
from avatar.bootstrap.container import AvatarContainer
from avatar.bootstrap.lifespan import create_lifespan

__all__ = ["AvatarContainer", "create_app", "create_lifespan"]