"""Utterance — Outlines-guided structured LLM turn for avatar actions."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AvatarAction(str, Enum):
    wave = "wave"
    smile = "smile"
    point = "point"
    neutral = "neutral"
    angry = "angry"
    happy = "happy"
    nod = "nod"


class Utterance(BaseModel):
    """One host turn: speech + deterministic avatar action."""

    speech: str = Field(..., min_length=1)
    action: AvatarAction = AvatarAction.neutral
    product_id: Optional[str] = None
    is_final: bool = False

    @classmethod
    def json_schema_for_guided(cls) -> dict:
        """JSON Schema suitable for Outlines / response_format guided decoding."""
        return cls.model_json_schema()
