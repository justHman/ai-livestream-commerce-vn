"""backend.api.dependencies — canonical typed access to application dependencies.

REST access:   ``request.app.state.container``
WS access:     ``websocket.app.state.container`` (before accept)
Lifespan:      receives its owning container explicitly.

These helpers replace the mutable process-global ``v1.deps()`` singleton for
canonical routes.  Two apps built with different containers remain isolated
because resolution always goes through the request/websocket's own app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request, WebSocket

if TYPE_CHECKING:
    from backend.bootstrap.container import BootstrapContainer


def container_from_request(request: Request) -> "BootstrapContainer":
    """Return the typed container attached to this request's app."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("application state has no container")
    return container


def container_from_websocket(websocket: WebSocket) -> "BootstrapContainer":
    """Return the typed container attached to this websocket's app.

    Safe to call BEFORE ``await websocket.accept()``; the app/state are part
    of the ASGI scope and exist as soon as the websocket is handed to the
    handler.
    """
    container = getattr(websocket.app.state, "container", None)
    if container is None:
        raise RuntimeError("application state has no container")
    return container