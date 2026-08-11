"""TTS engine lifecycle — bounded startup/shutdown in the ASGI lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tts.engines.base import TTSEngine, load_engine


def _build_engine() -> TTSEngine:
    """Construct the active self-host TTS engine from validated config."""
    from tts.config import load_engine_config

    cfg = load_engine_config()
    if cfg.engine == "none":
        return load_engine({})  # deterministic tone engine
    return load_engine(cfg.to_cfg_dict())


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run one configured TTS engine, bounded to the application lifetime."""
    engine = _build_engine()
    app.state.engine = engine
    app.state.engine_ready = True
    # With no provider runtime wired yet, the engine alone satisfies
    # readiness; the runtime cluster additionally gates runtime_ready.
    app.state.runtime_ready = True
    try:
        yield {"engine": engine}
    finally:
        app.state.engine_ready = False
        app.state.runtime_ready = False
        try:
            engine.unload()
        finally:
            app.state.engine = None
