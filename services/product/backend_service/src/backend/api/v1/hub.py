"""backend.api.v1.hub — control-plane WS hub + in-memory avatar store.

Copied from ``core/api/v1/router.py`` (COPY-DON'T-IMPORT, OpenSpec 1.21) so
the canonical backend service is self-contained without the legacy v1
router. ``ControlHub`` fans session events to the connected WebSocket;
``AvatarStore`` is the MVP in-memory avatar registry.

The full v1 route set (``backend.api.v1``) registers on the shared router;
this module holds only the shared state objects the canonical container wires.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Optional

from fastapi import WebSocket

__all__ = ["ControlHub", "AvatarStore"]


class ControlHub:
    """One WebSocket connection per session; emit() fans out events."""

    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id] = ws

    def disconnect(self, session_id: str) -> None:
        self._conns.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        ws = self._conns.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(session_id)

    async def broadcast(self, event: dict) -> None:
        """Send an event to ALL connected sessions (engine swap notifications)."""
        dead = []
        for sid, ws in self._conns.items():
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.disconnect(sid)


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
