"""Read-only live-follow snapshot the API reconciles from (contract: docs/live-follow-events.md)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.api.dependencies import container_from_request

from .router import router, viewer_auth


@router.get("/sessions/{session_id}/live-follow")
async def get_live_follow(
    session_id: str, request: Request, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    d = container_from_request(request)
    coordinator = d.coordinator
    if coordinator is None or not coordinator.has(session_id):
        raise HTTPException(status_code=404, detail={"code": "unknown_session"})
    meta = await d.store.get(session_id)
    if meta and meta.get("execution_contract"):
        coordinator.live_follow.bind(session_id, meta["execution_contract"])
    snapshot = coordinator.live_follow_snapshot(session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_session"})
    return snapshot
