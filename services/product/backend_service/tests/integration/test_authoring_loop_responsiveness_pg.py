"""R2 (HIGH-2): real LLM calls must not block the event loop.

A slow provider call must not stall the control plane: while a background
generation job is inside an LLM call, a concurrent GET /health/live must
respond well under the call duration. Generation / regenerate / fix / batch
run their sync provider calls via ``asyncio.to_thread`` so the loop stays free
(responsive health, SSE, and cancellation).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig, TTSConfig

from integration.authoring_helpers import FakeLlm, gate_compliant_text

# Provider latency per call. The health probe must answer well under this.
_SLOW_LLM_DELAY = 1.5
# Let the background job settle into its first (slow) provider call before the
# concurrent probe.
_SETTLE_SECONDS = 0.2


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        app_env="dev",
        render_backend="mock",
        database_url=database_url,
        tts=TTSConfig(engine="tone"),
    )


def _wire_slow_llm(app, llm) -> None:
    em = app.state.container.engine_manager
    em.llm_cfg["engine"] = "echo"  # _require_llm treats a non-"none" engine as available
    em.get_llm_fn = lambda: llm


@pytest.mark.asyncio
async def test_slow_provider_call_does_not_block_health(pg_url: str) -> None:
    from backend.main import create_app

    app = create_app(config=_config(pg_url))
    slow = FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=_SLOW_LLM_DELAY,
    )
    _wire_slow_llm(app, slow)

    with TestClient(app) as client:
        resp = client.post("/api/v1/script-sets", json={"name": "x", "product_ids": ["P1"]})
        assert resp.status_code == 201, resp.text
        set_id = resp.json()["id"]

        gen = client.post(
            f"/api/v1/script-sets/{set_id}/products/P1/generate",
            json={"target_duration_s": 600, "intent": "selling"},
        )
        assert gen.status_code == 202, gen.text

        # The background job is now running; give it time to enter its first
        # slow provider call, then probe the control plane concurrently.
        time.sleep(_SETTLE_SECONDS)
        started = time.perf_counter()
        health = client.get("/health/live")
        latency = time.perf_counter() - started
        assert health.status_code == 200
        assert latency < 1.0, f"/health/live took {latency:.2f}s — event loop blocked by LLM"
