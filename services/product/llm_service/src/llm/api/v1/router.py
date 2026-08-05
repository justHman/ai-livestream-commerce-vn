"""Versioned v1 route registration."""

from __future__ import annotations

from fastapi import APIRouter

from llm.api.v1.routes.chat_completions import router as chat_router
from llm.api.v1.routes.models import router as models_router

router = APIRouter()
router.include_router(models_router)
router.include_router(chat_router)
