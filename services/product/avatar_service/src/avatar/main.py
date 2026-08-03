"""Avatar service — self-host AvatarForcing with LiveKit publishing.
Entrypoint: uvicorn avatar.main:app
"""

from avatar.bootstrap.app_factory import create_app

app = create_app()