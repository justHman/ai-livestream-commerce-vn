"""LLM engine lifecycle — bounded startup/shutdown in the ASGI lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from llm.engines.base import LLMEngine, load_engine


def _build_engine() -> LLMEngine:
    """Construct the active self-host engine from validated config.

    Raises at startup when the engine cannot be built so readiness stays
    false while liveness remains true.
    """
    from llm.config import load_engine_config

    cfg = load_engine_config()
    if cfg.engine == "none":
        return load_engine({})  # deterministic noop engine
    return load_engine(cfg.to_cfg_dict())


@asynccontextmanager
async def create_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    """Run one configured engine, bounded to the application lifetime."""
    engine = _build_engine()
    app.state.engine = engine
    app.state.engine_ready = True
    try:
        yield {"engine": engine}
    finally:
        app.state.engine_ready = False
        try:
            engine.unload()
        finally:
            app.state.engine = None