"""Compatibility import for the canonical TTS engine package."""

from tts.engines.base import *  # noqa: F403

from .adapters import elevenlabs as _elevenlabs  # noqa: F401
from .adapters import openai_speech as _openai_speech  # noqa: F401
from .adapters import remote_http as _remote_http  # noqa: F401
