"""Per-session TTS voice override shared by /lite/say and the live Director."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SessionTTS:
    """Apply the validated voice override to this session without changing shared state."""

    def __init__(self, tts: Any, *, voice_id: str | None) -> None:
        self._tts = tts
        self._voice_id = voice_id

    def stream_audio(self, chunk: Any, **kwargs: Any):
        from tts.engines.base import TTSRequest

        kwargs["req"] = TTSRequest(text=chunk.text, voice=self._voice_id)
        yield from self._tts.stream_audio(chunk, **kwargs)


def voice_available(engine_manager: Any, voice_id: str) -> bool:
    """False only when the engine enumerates voices and omits ``voice_id``."""
    try:
        voices = engine_manager.voices()
    except Exception:  # noqa: BLE001 - unknown voice list: trust the stored id
        return True
    if all(v.get("label") == "Default" for v in voices):
        return True  # engine does not enumerate voices
    return voice_id in {str(v.get("id")) for v in voices}


def wrap_session_tts(tts: Any, voice_id: str | None, engine_manager: Any = None) -> Any:
    """Return ``tts`` bound to the session voice, or unchanged when absent/unavailable."""
    if tts is None or not voice_id:
        return tts
    if engine_manager is not None and not voice_available(engine_manager, voice_id):
        logger.warning("session voice not available, using default voice_id=%s", voice_id)
        return tts
    return SessionTTS(tts, voice_id=voice_id)
