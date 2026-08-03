"""OpenAI-compatible model discovery route.

The model list reflects the active self-host engine — never a hosted
adapter, which the LLM service does not select (Task 1.33).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from llm.api.dependencies import get_engine
from llm.api.security.authorization import require_scope
from llm.api.v1.schemas.common import ModelInfo, ModelListResponse
from llm.engines.base import LLMEngine

router = APIRouter()


@router.get("/models", response_model=ModelListResponse)
def list_models(
    _scope: str = Depends(require_scope("llm.models")),
    engine: LLMEngine = Depends(get_engine),
) -> ModelListResponse:
    """Return the models served by the active engine."""
    return ModelListResponse(
        data=[
            ModelInfo(
                id=engine.name,
                owned_by="self-host",
                created=int(time.time()),
                engine=engine.name,
            )
        ]
    )