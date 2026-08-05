"""Avatar discovery route for the avatar service.

Lists the avatars available on the active self-host engine only.
"""

from __future__ import annotations


from fastapi import APIRouter, Depends

from avatar.api.dependencies import get_engine
from avatar.api.security.authorization import require_scope
from avatar.api.v1.schemas.avatars import AvatarInfo, AvatarListResponse
from avatar.engines.base import AvatarEngine

router = APIRouter()


@router.get("/avatars", response_model=AvatarListResponse)
def list_avatars(
    _scope: str = Depends(require_scope("avatar.render")),
    engine: AvatarEngine = Depends(get_engine),
) -> AvatarListResponse:
    """Return the avatars available on the active self-host engine."""
    return AvatarListResponse(
        data=[
            AvatarInfo(
                id="default",
                name="Default avatar",
                engine=engine.name,
                status="available",
                description=f"Active engine: {engine.name}",
            )
        ]
    )
