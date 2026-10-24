"""Per-session TTS voice override shared by /lite/say and the live Director."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)


_VOICE_REJECTED = re.compile(r"HTTP (400|404|422)\b")


def _voice_rejected(exc: Exception) -> bool:
    """TTS refused the request itself (unknown/removed voice), not a transient outage."""
    from backend.application.clients.tts.self_hosted import TTSClientError

    return isinstance(exc, TTSClientError) and bool(_VOICE_REJECTED.search(str(exc)))


class SessionTTS:
    """Apply the validated voice override to this session without changing shared state.

    Fallback to the default voice only covers a rejection raised before the first audio
    window of a chunk; later errors propagate unchanged. If the default voice fails too,
    the error propagates and the session voice is retried next chunk.
    """

    def __init__(
        self,
        tts: Any,
        *,
        voice_id: str | None,
        on_reject: Callable[[], None] | None = None,
    ) -> None:
        self._tts = tts
        self._voice_id = voice_id
        self._on_reject = on_reject

    def stream_audio(self, chunk: Any, **kwargs: Any):
        from tts.engines.base import TTSRequest

        if self._voice_id is None:
            kwargs["req"] = TTSRequest(text=chunk.text, voice=None)
            yield from self._tts.stream_audio(chunk, **kwargs)
            return
        req = TTSRequest(text=chunk.text, voice=self._voice_id)
        stream = self._tts.stream_audio(chunk, **{**kwargs, "req": req})
        try:
            first = next(stream)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 - only a voice rejection before any audio falls back
            if not _voice_rejected(exc):
                raise
            fallback = self._tts.stream_audio(
                chunk, **{**kwargs, "req": TTSRequest(text=chunk.text, voice=None)}
            )
            try:
                first = next(fallback)
            except StopIteration:
                return
            # Default voice worked, so the session voice was the culprit: remember it.
            logger.warning(
                "session voice rejected by TTS, using default voice_id=%s", self._voice_id
            )
            self._voice_id = None
            if self._on_reject is not None:
                self._on_reject()
            yield first
            yield from fallback
            return
        yield first
        yield from stream


def voice_available(engine_manager: Any, voice_id: str) -> bool:
    """False only when the engine enumerates voices and omits ``voice_id``."""
    try:
        voices = engine_manager.voices()
    except Exception:  # noqa: BLE001 - unknown voice list: trust the stored id
        return True
    if all(v.get("label") == "Default" for v in voices):
        return True  # engine does not enumerate voices
    return voice_id in {str(v.get("id")) for v in voices}


def wrap_session_tts(
    tts: Any,
    voice_id: str | None,
    engine_manager: Any = None,
    on_reject: Callable[[], None] | None = None,
) -> Any:
    """Return ``tts`` bound to the session voice, or unchanged when absent/unavailable."""
    if tts is None or not voice_id:
        return tts
    if engine_manager is not None and not voice_available(engine_manager, voice_id):
        logger.warning("session voice not available, using default voice_id=%s", voice_id)
        return tts
    return SessionTTS(tts, voice_id=voice_id, on_reject=on_reject)
