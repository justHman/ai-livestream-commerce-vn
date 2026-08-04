"""core.api.v1 — /api/v1 public API surface (OpenSpec 1.20 split).

The v1 router is assembled in ``router.py`` from the route/schema modules
(``sessions``, ``avatars``, ``voices``, ``admin``, ``websockets``) and the
shared wiring/helpers in ``router.py`` itself. ``core.api.v1`` re-exports the
router plus the shared names so existing imports (``from core.api import v1``
/ ``from core.api.v1 import ...``) keep working unchanged.
"""

from __future__ import annotations

import sys as _sys

from . import admin, avatars, sessions, voices, websockets  # noqa: F401  (register routes on the shared router)

import core.api.v1.router as _router_module  # noqa: F401  (bind submodule first)

from ..auth import admin_auth, debug_enabled_dep, validate_ws_token, viewer_auth  # noqa: F401
from .sessions import lite_say, lite_start, lite_stop  # noqa: F401
from .router import (  # noqa: F401
    AvatarCreateReq,
    AvatarStore,
    ChatIn,
    CommentIn,
    ControlHub,
    HTTPException,
    IngestReq,
    PlanCreateReq,
    ProductIn,
    SayReq,
    SessionReq,
    StartReq,
    V1Deps,
    SANDBOX_LAYER_TIMEOUT_SEC,
    _persist_viewer_msgs,
    build_run_plan,
    deps,
    init_deps,
    router,
)

# FastAPI/starlette re-exported for parity with the pre-split module.
from .router import (
    _mock_backend,  # noqa: F401
    mock_video_mjpeg,  # noqa: F401
)

# Bind the router module object under its own name so ``import
# core.api.v1.router`` resolves the submodule (not the APIRouter attribute
# bound by the re-export above). Keeps test ``from core.api.v1 import ...``
# working while ``import core.api.v1.router as r`` stays the module.
_sys.modules[__name__ + ".router"] = _sys.modules["core.api.v1.router"]
