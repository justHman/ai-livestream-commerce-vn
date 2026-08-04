"""backend.api.v1 — canonical /api/v1 control-plane surface (Task 1.25).

Self-contained copy of the legacy ``core.api.v1`` route set (COPY-DON'T-
IMPORT). Route modules register on the shared router in ``router.py``; the
``__init__`` imports them so importing ``backend.api.v1`` assembles the full
route table. Dependencies come from the request-scoped ``BootstrapContainer``.

Production contract: no ``/lite/*``, ``/debug/*``, ``/mock/*`` or sandbox
routes. Health lives outside the versioned contract (``backend.api.health``).
"""

from __future__ import annotations

from . import admin, avatars, sessions, voices, websockets  # noqa: F401  (register routes on the shared router)

from .router import (  # noqa: F401
    AvatarCreateReq,
    ChatIn,
    CommentIn,
    ControlHub,
    IngestReq,
    PlanCreateReq,
    ProductIn,
    SayReq,
    SessionReq,
    StartReq,
    TTSPresetIn,
    TTSPreviewReq,
    _persist_viewer_msgs,
    build_run_plan,
    router,
)

from .hub import AvatarStore  # noqa: F401

__all__ = [
    "AvatarCreateReq",
    "AvatarStore",
    "ChatIn",
    "CommentIn",
    "ControlHub",
    "IngestReq",
    "PlanCreateReq",
    "ProductIn",
    "SayReq",
    "SessionReq",
    "StartReq",
    "TTSPresetIn",
    "TTSPreviewReq",
    "build_run_plan",
    "router",
]
