from __future__ import annotations
from pathlib import Path
import sys


_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Canonical sibling service packages (llm.*, tts.*, avatar.*) — same
# PYTHONPATH layout the backend Dockerfile uses. COPY-DON'T-IMPORT: these
# packages are the canonical self-contained copies, not core shims.
_PRODUCT = Path(__file__).resolve().parents[2]
for _sibling in ("llm_service", "tts_service", "avatar_service"):
    _path = str(_PRODUCT / _sibling / "src")
    if _path not in sys.path:
        sys.path.insert(0, _path)


def make_deps(
    *,
    backend=None,
    store=None,
    hub=None,
    config=None,
    director=None,
    engine_manager=None,
    coordinator=None,
    pg_store=None,
    livekit_publishers=None,
    avatars=None,
    orchestrators=None,
    locks=None,
    event_ingestion=None,
):
    """Build the deps-shaped object accepted by ``create_app(deps=...)``.

    Mirrors the legacy V1Deps attributes into the typed BootstrapContainer
    without mutating global env (OpenSpec 1.51).
    """
    from backend.api.v1.hub import AvatarStore, ControlHub
    from backend.application.db import InMemorySessionStore
    from backend.application.render.mock import MockRenderBackend
    from backend.engine_manager import EngineManager

    class _Deps:
        def __init__(self) -> None:
            self.backend = backend if backend is not None else MockRenderBackend()
            self.store = store if store is not None else InMemorySessionStore()
            self.hub = hub if hub is not None else ControlHub()
            self.config = config
            self.director = director
            self.engine_manager = engine_manager if engine_manager is not None else EngineManager()
            self.coordinator = coordinator
            self.pg_store = pg_store
            self.livekit_publishers = livekit_publishers
            self.avatars = avatars if avatars is not None else AvatarStore()
            from backend.application.render.locks import SessionLockRegistry

            self.locks = locks if locks is not None else SessionLockRegistry()
            self.orchestrators = orchestrators if orchestrators is not None else {}
            self.event_ingestion = event_ingestion

    return _Deps()
