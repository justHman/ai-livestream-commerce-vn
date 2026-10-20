"""backend.api.v1.tts_voices — GET /tts/voices: voices offered by the self-hosted TTS server.

Viewer bearer AND the internal token (same gate as per-session voice overrides). A TTS server that is
off or answers garbage is "unavailable", never a 5xx, so the merchant UI can simply hide the picker.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request
from backend.application.clients.tts.self_hosted import SelfHostedTTSClient, TTSClientError

from .auth import viewer_auth
from .router import logger, router
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


@router.get("/tts/voices")
async def tts_voices(request: Request, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    cfg = container_from_request(request).config
    if not _authenticated_internal_caller(request, cfg):
        raise HTTPException(status_code=403, detail={"code": "voices_not_authorized"})
    if cfg.tts.engine != "remote_http":
        return {"voices": [], "available": False}
    voices = await asyncio.to_thread(
        _cached_voices, cfg.tts.base_url, (cfg.tts.extra or {}).get("api_key", "")
    )
    return {"voices": voices or [], "available": voices is not None}
