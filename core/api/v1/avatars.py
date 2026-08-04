"""core.api.v1.avatars — in-memory avatar CRUD + idle regenerate."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Optional

from fastapi import Depends, HTTPException

from ..auth import viewer_auth
from .router import router, AvatarCreateReq, AvatarUpdateReq, deps


class AvatarStore:
    """Thread-safe in-memory avatar registry (MVP; no DB)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def create(
        self,
        *,
        scope: str,
        ref_photo_url: Optional[str],
        voice: Optional[str],
    ) -> dict[str, Any]:
        avatar_id = str(uuid.uuid4())
        item = {
            "avatar_id": avatar_id,
            "id": avatar_id,
            "label": f"Custom avatar {avatar_id[:8]}",
            "scope": scope,
            "ref_photo_url": ref_photo_url,
            "thumbnail_url": ref_photo_url,
            "voice": voice,
            "status": "ready",
            "ready": True,
            "capabilities": ["speech", "idle", scope],
        }
        with self._lock:
            self._items[avatar_id] = item
        return dict(item)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._items.values()]

    def get(self, avatar_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._items.get(avatar_id)
            return dict(item) if item is not None else None

    def update(self, avatar_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._items.get(avatar_id)
            if item is None:
                return None
            for k, v in fields.items():
                if v is not None and k in item:
                    item[k] = v
            return dict(item)

    def delete(self, avatar_id: str) -> bool:
        with self._lock:
            return self._items.pop(avatar_id, None) is not None


# ── Wiring (set by core/server.py) ──────────────────────────────────


@router.post("/avatars")
async def avatars_create(req: AvatarCreateReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    return deps().avatars.create(
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )


@router.get("/avatars")
async def avatars_list(_: None = Depends(viewer_auth)) -> dict[str, Any]:
    return {"avatars": deps().avatars.list()}


@router.get("/avatars/{avatar_id}")
async def avatars_get(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    item = deps().avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.put("/avatars/{avatar_id}")
async def avatars_put(
    avatar_id: str, req: AvatarUpdateReq, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    item = deps().avatars.update(
        avatar_id,
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.delete("/avatars/{avatar_id}")
async def avatars_delete(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    ok = deps().avatars.delete(avatar_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return {"ok": True, "deleted": avatar_id}


@router.post("/avatars/{avatar_id}/idle/regenerate")
async def avatars_idle_regenerate(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    item = deps().avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    # Stub: real idle pre-render is avatar-server work.
    return {"ok": True, "avatar_id": avatar_id, "status": "ready", "frames": 75}
