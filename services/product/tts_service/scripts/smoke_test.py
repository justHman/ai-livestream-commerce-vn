"""Smoke-test the canonical TTS package import."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tts import TTSEngine  # noqa: E402,I001

assert TTSEngine.__name__ == "TTSEngine"
print("tts import: ok")
