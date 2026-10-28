"""backend.api.v1.tts_voices — GET /tts/voices: voices offered by the self-hosted TTS server.

Viewer bearer AND the internal token (same gate as per-session voice overrides). A TTS server that is
off or answers garbage is "unavailable", never a 5xx, so the merchant UI can simply hide the picker.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.api.dependencies import container_from_request
from backend.application.clients.tts import endpoint_override
from backend.application.clients.tts.self_hosted import SelfHostedTTSClient, TTSClientError

from .auth import viewer_auth
from .router import logger, rate_limit_viewer, router
from .sessions import _authenticated_internal_caller

_OK_TTL_S = 60.0
_FAIL_TTL_S = 10.0
# ponytail: per-process cache of the last answer, keyed by the configured upstream; one lock coalesces concurrent misses.
# (base_url, expires_at monotonic, voices or None when the fetch failed)
_cache: tuple[str, float, list[dict] | None] | None = None
_lock = threading.Lock()


def _fetch_voices(base_url: str, api_key: str) -> list[dict]:
    client = SelfHostedTTSClient(base_url=base_url, api_key=api_key)
    try:
        return client.list_voices()
    finally:
        client.close()


def _cached_voices(base_url: str, api_key: str) -> list[dict] | None:
    """Voices of the TTS server, or None when it is unavailable. Runs in a worker thread."""
    global _cache
    with _lock:
        now = time.monotonic()
        if _cache is None or _cache[0] != base_url or _cache[1] <= now:
            try:
                voices: list[dict] | None = _fetch_voices(base_url, api_key)
                ttl = _OK_TTL_S
            except TTSClientError as exc:
                logger.warning("tts_voices_unavailable: %s", exc)
                voices, ttl = None, _FAIL_TTL_S
            _cache = (base_url, now + ttl, voices)
        return _cache[2]


def _voices_for(cfg: Any) -> list[dict] | None:
    # The override lookup may wait on Redis, so it runs in the worker thread with the fetch, never on the event loop.
    return _cached_voices(
        endpoint_override.effective_base_url(cfg.tts.base_url),
        (cfg.tts.extra or {}).get("api_key", ""),
    )


@router.get("/tts/voices")
async def tts_voices(request: Request, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    cfg = container_from_request(request).config
    if not _authenticated_internal_caller(request, cfg):
        raise HTTPException(status_code=403, detail={"code": "voices_not_authorized"})
    if cfg.tts.engine != "remote_http":
        return {"voices": [], "available": False}
    voices = await asyncio.to_thread(_voices_for, cfg)
    return {"voices": voices or [], "available": voices is not None}


class VoicePreviewReq(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    voice_id: str = Field(min_length=1, max_length=512)


_PREVIEW_TIMEOUT_S = 30.0
# A preview shares the TTS server with live speech: one at a time, a second one is turned away (never queued).
_preview_slot = asyncio.Semaphore(1)


def _finish_preview(task: "asyncio.Future[Any]") -> None:
    """Free the preview slot when the worker ends; a failure nobody awaited is only logged by type."""
    _preview_slot.release()
    if not task.cancelled() and task.exception() is not None:
        logger.info("tts_preview_worker_ended error_type=%s", type(task.exception()).__name__)


@router.post("/tts/preview")
async def tts_preview(
    payload: VoicePreviewReq,
    request: Request,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> Response:
    """WAV of a voice from the offered list saying a short text; same trust gate as GET /tts/voices."""
    d = container_from_request(request)
    cfg = d.config
    if not _authenticated_internal_caller(request, cfg):
        raise HTTPException(status_code=403, detail={"code": "voices_not_authorized"})
    manager = d.engine_manager
    if cfg.tts.engine != "remote_http" or manager is None or manager.tts is None:
        raise HTTPException(status_code=503, detail={"code": "tts_unavailable"})
    voices = await asyncio.to_thread(_voices_for, cfg)
    if voices is None:
        raise HTTPException(status_code=503, detail={"code": "tts_unavailable"})
    if payload.voice_id not in {voice.get("id") for voice in voices}:
        raise HTTPException(status_code=400, detail={"code": "unknown_voice"})
    if _preview_slot.locked():
        raise HTTPException(status_code=429, detail={"code": "preview_busy"})

    from tts.engines.base import TTSRequest

    # The slot is held until the worker thread really ends, not until the request gives up: a timed-out
    # synthesis keeps running on the shared TTS server and must still count as the one running preview.
    await _preview_slot.acquire()
    task = asyncio.ensure_future(
        asyncio.to_thread(
            manager.tts.synthesize, TTSRequest(text=payload.text, voice=payload.voice_id)
        )
    )
    task.add_done_callback(_finish_preview)
    try:
        audio = await asyncio.wait_for(asyncio.shield(task), timeout=_PREVIEW_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail={"code": "preview_timeout"}) from exc
    except Exception as exc:  # noqa: BLE001 - any engine failure is "preview unavailable"
        logger.warning("tts_preview_failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail={"code": "preview_failed"}) from exc

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio.sample_rate)
        wav.writeframes(audio.to_pcm16_bytes())
    return Response(content=output.getvalue(), media_type="audio/wav")
