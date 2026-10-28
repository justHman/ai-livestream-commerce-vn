"""backend.api.v1.tts_endpoint — the operator sets the address of the self-host TTS server.

Called only by the platform admin service with its own secret (X-Livento-Tts-Admin = TTS_ADMIN_TOKEN); neither the viewer
bearer nor the avatar/voice internal token can reach it. 404 while the feature is off (no TTS_ADMIN_TOKEN / REDIS_URL).
A new address is probed (GET /v1/voices) before it is stored, so a typo or a dead GPU is refused, not saved.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request

from backend.api.dependencies import container_from_request
from backend.api.security.authentication import tokens_match
from backend.application.clients.tts import endpoint_override

from .router import logger, router

_ACTOR = re.compile(r"^[\x20-\x7e]{1,64}$")
_PROBE_TIMEOUT_S = 8.0


def _guard(request: Request) -> str:
    if not endpoint_override.enabled():
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    presented = (request.headers.get("x-livento-tts-admin") or "").strip()
    if not presented or not tokens_match(presented, endpoint_override.admin_token()):
        raise HTTPException(status_code=403, detail={"code": "tts_admin_forbidden"})
    actor = (request.headers.get("x-livento-actor") or "").strip()
    if not _ACTOR.match(actor):
        raise HTTPException(status_code=400, detail={"code": "actor_required"})
    return actor


def _config(request: Request):
    cfg = container_from_request(request).config
    if cfg.tts.engine != "remote_http":
        raise HTTPException(status_code=409, detail={"code": "tts_not_remote"})
    return cfg


async def _probe(base_url: str, api_key: str) -> bool:
    """Headers only: the status line is the proof the server answers. Async, so the caller's deadline really cancels the
    socket I/O (a server dripping headers cannot hold anything past _PROBE_TIMEOUT_S)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0), follow_redirects=False) as client:
            async with client.stream(
                "GET", urljoin(base_url + "/", "v1/voices"), headers=headers
            ) as resp:
                return resp.status_code == 200
    except (httpx.HTTPError, httpx.InvalidURL):
        return False


async def _reachable(base_url: str, api_key: str) -> bool:
    try:
        return await asyncio.wait_for(_probe(base_url, api_key), timeout=_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return False


async def _state(cfg, probe: bool = True) -> dict[str, Any]:
    record = await asyncio.to_thread(endpoint_override.read_override)
    base_url = record["base_url"] if record else cfg.tts.base_url
    api_key = (cfg.tts.extra or {}).get("api_key", "")
    return {
        "base_url": base_url,
        "source": "override" if record else "env",
        "env_base_url": cfg.tts.base_url,
        "updated_by": record["updated_by"] if record else None,
        "updated_at": record["updated_at"] if record else None,
        "reachable": await _reachable(base_url, api_key) if probe else True,
    }


@router.get("/tts/endpoint")
async def get_tts_endpoint(request: Request) -> dict[str, Any]:
    _guard(request)
    cfg = _config(request)
    try:
        return await _state(cfg)
    except Exception as exc:  # noqa: BLE001 - Redis down: say so instead of a 500
        logger.warning("tts_endpoint_read_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "tts_endpoint_store_unavailable"}
        ) from exc


@router.put("/tts/endpoint")
async def put_tts_endpoint(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    actor = _guard(request)
    cfg = _config(request)
    try:
        base_url = endpoint_override.validate_base_url(str(payload.get("base_url", "")))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_base_url", "reason": str(exc)}
        ) from exc
    if not await _reachable(base_url, (cfg.tts.extra or {}).get("api_key", "")):
        raise HTTPException(status_code=422, detail={"code": "tts_unreachable"})
    try:
        await asyncio.to_thread(endpoint_override.write_override, base_url, actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tts_endpoint_write_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "tts_endpoint_store_unavailable"}
        ) from exc
    logger.info("tts_endpoint_changed actor=%s base_url=%s", actor, base_url)
    return await _state(cfg, probe=False)  # it answered the probe a moment ago


@router.delete("/tts/endpoint")
async def delete_tts_endpoint(request: Request) -> dict[str, Any]:
    actor = _guard(request)
    cfg = _config(request)
    try:
        await asyncio.to_thread(endpoint_override.clear_override)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tts_endpoint_clear_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "tts_endpoint_store_unavailable"}
        ) from exc
    logger.info("tts_endpoint_cleared actor=%s", actor)
    return await _state(cfg)
