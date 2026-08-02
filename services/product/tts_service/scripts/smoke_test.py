"""Smoke-test the canonical TTS package import."""

from tts import TTSEngine

assert TTSEngine.__name__ == "TTSEngine"
print("tts import: ok")
