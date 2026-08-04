"""backend.api.v1.avatars — in-memory avatar CRUD + idle regenerate.

Copied from ``core/api/v1/avatars.py`` (COPY-DON'T-IMPORT, Task 1.25);
dependencies come from the typed ``BootstrapContainer``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request

from .auth import viewer_auth
from .router import router, AvatarCreateReq, AvatarUpdateReq


@router.post("/avatars")
async def avatars_create(
    req: AvatarCreateReq, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    return container_from_request(request).avatars.create(
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )


@router.get("/avatars")
async def avatars_list(request: Request, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    return {"avatars": container_from_request(request).avatars.list()}


@router.get("/avatars/{avatar_id}")
async def avatars_get(
    avatar_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    item = container_from_request(request).avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.put("/avatars/{avatar_id}")
async def avatars_put(
    avatar_id: str,
    req: AvatarUpdateReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    item = container_from_request(request).avatars.update(
        avatar_id,
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.delete("/avatars/{avatar_id}")
async def avatars_delete(
    avatar_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    ok = container_from_request(request).avatars.delete(avatar_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return {"ok": True, "deleted": avatar_id}


@router.post("/avatars/{avatar_id}/idle/regenerate")
async def avatars_idle_regenerate(
    avatar_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    item = container_from_request(request).avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    # Stub: real idle pre-render is avatar-server work.
    return {"ok": True, "avatar_id": avatar_id, "status": "ready", "frames": 75}
