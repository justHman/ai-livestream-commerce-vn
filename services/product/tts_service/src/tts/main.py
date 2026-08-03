"""TTS service — self-host VieNeu/CosyVoice synthesis.
Entrypoint: uvicorn tts.main:app
"""

from tts.bootstrap.app_factory import create_app

app = create_app()