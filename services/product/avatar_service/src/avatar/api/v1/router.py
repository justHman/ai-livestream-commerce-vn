"""Versioned v1 route registration for the avatar service."""

from __future__ import annotations

from fastapi import APIRouter

from avatar.api.v1.routes.avatars import router as avatars_router
from avatar.api.v1.routes.sessions import router as sessions_router

router = APIRouter()
router.include_router(avatars_router)
router.include_router(sessions_router)
