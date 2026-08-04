"""backend.api.v1.admin — admin config/health (canonical copy, Task 1.25).

Copied from ``core/api/v1/admin.py`` (COPY-DON'T-IMPORT) minus the debug-mode
routes (``/debug/*`` — mock traffic simulation is dev tooling and is excluded
from the production route table) and the sandbox verifier (a provider-layer
probe that belongs to the avatar service; cloud credential verification runs
in ``avatar_service``).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request
from backend.config import AppConfig

from .auth import admin_auth
from .router import router


@router.get("/admin/config")
async def admin_config(request: Request, _: None = Depends(admin_auth)) -> dict[str, Any]:
    """Sanitized config dump: present/missing for secrets, no secret values."""
    cfg = container_from_request(request).config or AppConfig.from_env()

    def _present(val: str) -> str:
        return "present" if (val or "").strip() else "missing"

    return {
        "app_env": cfg.app_env,
        "render_backend": cfg.render_backend,
        "store_backend": cfg.store_backend,
        "director_enabled": cfg.director_enabled,
        "debug_enabled": cfg.debug_enabled,
        "pipecat_enabled": getattr(cfg, "pipecat_enabled", False),
        "lmcache_enabled": cfg.lmcache_enabled,
        "coverage_match_threshold": getattr(cfg, "coverage_match_threshold", 0.75),
        "llm": {
            "engine": cfg.llm.engine,
            "model": cfg.llm.model or None,
            "base_url": "present" if cfg.llm.base_url else "missing",
            "guided_json": getattr(cfg.llm, "guided_json", False),
            "stream": cfg.llm.stream,
        },
        "tts": {
            "engine": cfg.tts.engine,
            "model": cfg.tts.model or None,
            "base_url": "present" if cfg.tts.base_url else "missing",
            "preset_id": cfg.tts.preset_id,
        },
        "secrets": {
            "backend_api_token": _present(cfg.backend_api_token),
            "admin_api_token": _present(cfg.admin_api_token),
            "liveavatar_api_key": "present" if cfg.api_key_present else "missing",
            "livekit_api_key": _present(cfg.livekit_api_key),
            "livekit_api_secret": _present(cfg.livekit_api_secret),
            "avatar_base_url": _present(cfg.avatar_base_url),
            "livekit_url": _present(cfg.livekit_url),
        },
    }


@router.get("/admin/health")
async def admin_health(request: Request, _: None = Depends(admin_auth)) -> dict[str, Any]:
    """Deep health — same payload as /health/ready."""
    from .router import health_ready

    return await health_ready()
