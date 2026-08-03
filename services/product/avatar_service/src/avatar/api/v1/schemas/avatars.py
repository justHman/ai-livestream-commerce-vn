"""Avatar discovery schemas.

Reflects the active self-host engine only (Task 1.33: hosted avatars such
as LiveAvatar or Baidu Xiling are backend adapter concerns).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AvatarInfo(BaseModel):
    id: str
    name: str = ""
    engine: str = ""
    status: str = "available"
    description: Optional[str] = None


class AvatarListResponse(BaseModel):
    object: str = "list"
    data: list[AvatarInfo] = Field(default_factory=list)
