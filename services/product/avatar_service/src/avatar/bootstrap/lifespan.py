"""Avatar container lifecycle — bounded startup/shutdown in the ASGI lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from avatar.bootstrap.container import AvatarContainer
from avatar.config import load_engine_config


def _build_container() -> AvatarContainer:
    from avatar.config import load_publishing_config

    engine_cfg = load_engine_config()
    publishing_cfg = load_publishing_config()
    return AvatarContainer(engine_cfg, publishing_cfg)


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run the container, bounded to the application lifetime."""
    container = _build_container()
    engine_cfg = load_engine_config()
    app.state.container = container
    app.state.engine = container.engine
    # Truthful readiness (R0.3/Decision 5): only a real self-host engine is
    # ready. AVATAR_ENGINE=none builds the AvatarForcingEngine(model="mock")
    # stub, which must never advertise production-ready self-host.
    app.state.engine_ready = engine_cfg.engine not in ("", "none")
    try:
        yield {"container": container}
    finally:
        app.state.engine_ready = False
        try:
            container.close()
        finally:
            app.state.engine = None
