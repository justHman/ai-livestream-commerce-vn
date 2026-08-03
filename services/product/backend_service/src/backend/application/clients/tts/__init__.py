"""TTS outbound clients (self-host service proxy, ElevenLabs, OpenAI Speech)."""

from backend.application.clients.tts.self_hosted import SelfHostedTTSClient
from backend.application.clients.tts.elevenlabs import ElevenLabsTTSClient
from backend.application.clients.tts.openai_speech import OpenAISpeechTTSClient

__all__ = [
    "ElevenLabsTTSClient",
    "OpenAISpeechTTSClient",
    "SelfHostedTTSClient",
]