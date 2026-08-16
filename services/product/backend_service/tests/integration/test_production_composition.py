"""P0-01: production composition wires the agentic Director pipeline.

Boots the REAL ``create_app(config=AppConfig.from_env())`` path (no deps
injection) with ``DIRECTOR_ENABLED=1`` and asserts the composition root owns
DirectorRuntime / DirectorCoordinator / FastReducer, injects the SAME
instances into ``PlatformEventIngestionService``, the reducer loop task is
started by the lifespan and cancelled at shutdown, and the fast lane wakes on
an accepted comment.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig


def _event(text: str, author: str, event_id: str, ts: float | None = None) -> dict:
    return {
        "event_id": event_id,
        "platform": "tiktok",
        "source_stream_id": "stream-1",
        "occurred_at": ts if ts is not None else time.time(),
        "type": "viewer.comment",
        "viewer": {"viewer_id": author, "display_name": author},
        "payload": {"text": text},
    }


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "1")
    monkeypatch.setenv("APP_ENV", "dev")


def test_production_composition_wires_director_pipeline(prod_env: None) -> None:
    from backend.main import create_app

    app = create_app(config=AppConfig.from_env())
    with TestClient(app) as client:
        container = app.state.container

        # Composition root owns the Director pipeline.
        assert container.director is not None
        assert container.coordinator is not None
        # The SAME instances are injected into the ingestion service.
        assert container.event_ingestion._coordinator is container.coordinator
        assert container.event_ingestion._reducer is not None

        # start -> attach returns 200 (not 501).
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        r = client.post(f"/api/v1/sessions/{sid}/attach", json={"products": []})
        assert r.status_code == 200, r.text

        # One viewer.comment is accepted and wakes the fast lane.
        r = client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"events": [_event("Gia bao nhieu?", "viewer", "prod-evt-1")]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] == 1

        reducer = container.event_ingestion._reducer
        assert reducer.stats(sid)["wake_notifications"] >= 1

        # The lifespan started a reducer-loop task named reducer-*.
        task = container.reducer_loop_task
        assert task is not None
        assert task.get_name().startswith("reducer")
        assert not task.done()

    # After TestClient exit, the reducer loop task was cancelled at shutdown.
    assert container.reducer_loop_task is not None
    assert container.reducer_loop_task.done()
