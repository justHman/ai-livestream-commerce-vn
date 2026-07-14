"""Persist rows to the runtime DB at ingest/chat/decision sites (pg enabled).

Fake pg store records calls; no real DB. When pg_store is None or disabled,
the routes behave exactly as before (no persistence, no errors).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.server import create_app


class _FakePgStore:
    def __init__(self) -> None:
        self.enabled = True
        self.viewer_msgs: list = []
        self.director_decisions: list = []
        self.sessions: list = []
        self.snapshots: list = []

    async def connect(self):
        pass

    async def apply_schema(self):
        pass

    async def close(self):
        pass

    async def upsert_session(self, sid, **kw):
        self.sessions.append((sid, kw))

    async def insert_viewer_msg(self, sid, text, **kw):
        self.viewer_msgs.append((sid, text, kw))
        return len(self.viewer_msgs)

    async def insert_director_decision(self, sid, action, **kw):
        self.director_decisions.append((sid, action, kw))
        return len(self.director_decisions)

    async def insert_product_snapshot(self, sid, products):
        self.snapshots.append((sid, products))


def _app_with_pg(pg, monkeypatch) -> TestClient:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    deps = v1.V1Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=None,
        config=config,
        locks=None,
        orchestrators={},
        coordinator=None,
        pg_store=pg,
    )
    return TestClient(create_app(config=config, deps=deps))


def test_lite_start_persists_session(monkeypatch):
    pg = _FakePgStore()
    with _app_with_pg(pg, monkeypatch) as client:
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        assert r.status_code == 200
        sid = r.json()["session_id"]
    assert len(pg.sessions) == 1
    assert pg.sessions[0][0] == sid


def test_lite_ingest_no_pg_behaves_unchanged(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    deps = v1.V1Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        hub=v1.ControlHub(),
        director=None,
        engine_manager=None,
        config=config,
        locks=None,
        orchestrators={},
        coordinator=None,
        pg_store=None,
    )
    with TestClient(create_app(config=config, deps=deps)) as client:
        r = client.post(
            "/api/v1/lite/ingest",
            json={"session_id": "x", "comments": [], "viewer_count": 0, "msg_rate": 0},
        )
        # Director not enabled -> 501, but no persistence crash.
        assert r.status_code in (501, 409)
