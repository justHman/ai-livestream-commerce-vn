"""backend.api.v1.tts_voices — GET /tts/voices: voices offered by the self-hosted TTS server.

Viewer bearer AND the internal token (same gate as per-session voice overrides). A TTS server that is
off or answers garbage is "unavailable", never a 5xx, so the merchant UI can simply hide the picker.
"""

from __future__ import annotations

import asyncio
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
# ponytail: per-process cache, one entry (one TTS server per deployment); key by base_url if that changes.
# (expires_at monotonic, voices or None when the fetch failed)
_cache: tuple[float, list[dict] | None] | None = None


def _fetch_voices(base_url: str, api_key: str) -> list[dict]:
    client = SelfHostedTTSClient(base_url=base_url, api_key=api_key)
    try:
        return client.list_voices()
    finally:
        client.close()


@router.get("/tts/voices")
async def tts_voices(request: Request, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    global _cache
    cfg = container_from_request(request).config
    if not _authenticated_internal_caller(request, cfg):
        raise HTTPException(status_code=403, detail={"code": "voices_not_authorized"})
    if cfg.tts.engine != "remote_http":
        return {"voices": [], "available": False}
    now = time.monotonic()
    if _cache is None or _cache[0] <= now:
        try:
            voices = await asyncio.to_thread(
                _fetch_voices, cfg.tts.base_url, (cfg.tts.extra or {}).get("api_key", "")
            )
            _cache = (now + _OK_TTL_S, voices)
        except TTSClientError as exc:
            logger.warning("tts_voices_unavailable: %s", exc)
            _cache = (now + _FAIL_TTL_S, None)
    voices = _cache[1]
    return {"voices": voices or [], "available": voices is not None}
