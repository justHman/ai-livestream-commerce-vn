"""Avatar container lifecycle — bounded startup/shutdown in the ASGI lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from avatar.bootstrap.container import AvatarContainer


def _build_container() -> AvatarContainer:
    from avatar.config import load_engine_config, load_publishing_config

    engine_cfg = load_engine_config()
    publishing_cfg = load_publishing_config()
    return AvatarContainer(engine_cfg, publishing_cfg)


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run the container, bounded to the application lifetime."""
    container = _build_container()
    app.state.container = container
    app.state.engine = container.engine
    app.state.engine_ready = True
    try:
        yield {"container": container}
    finally:
        app.state.engine_ready = False
        try:
            container.close()
        finally:
            app.state.engine = None
