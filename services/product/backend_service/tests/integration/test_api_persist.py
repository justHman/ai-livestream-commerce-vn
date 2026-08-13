"""Persist event rows to the runtime DB at ingest sites (pg enabled).

Fake pg store records calls; no real DB. When pg_store is None or disabled,
the routes behave exactly as before (no persistence, no errors). The old
``_persist_viewer_msgs`` helper is removed (multi-platform change, Decision
22) — the PlatformEventIngestionService owns fire-and-forget persistence.
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient

from backend.application.platform_events import PlatformEventIngestionService
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.config import AppConfig
from backend.main import create_app


class _FakePgStore:
    def __init__(self) -> None:
        self.enabled = True
        self.viewer_msgs: list = []
        self.director_decisions: list = []
        self.sessions: list = []
        self.snapshots: list = []
        self.audit_events: list = []

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

    async def insert_audit_event(self, event_type, **kw):
        self.audit_events.append((event_type, kw))
        return len(self.audit_events)


def _app_with_pg(pg, monkeypatch) -> TestClient:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    store = config.build_store()
    deps = _Deps(
        backend=config.build_render_backend(),
        store=store,
        director=None,
        engine_manager=None,
        config=config,
        locks=None,
        orchestrators={},
        coordinator=None,
        pg_store=pg,
        event_ingestion=PlatformEventIngestionService(store=store, pg_store=pg),
    )
    return TestClient(create_app(config=config, deps=deps))


def _event(text: str, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "platform": "shopee",
        "source_stream_id": "stream-9",
        "occurred_at": time.time(),
        "type": "viewer.comment",
        "viewer": {"viewer_id": "u1", "display_name": "u1"},
        "payload": {"text": text},
    }


def test_lite_start_persists_session(monkeypatch):
    pg = _FakePgStore()
    with _app_with_pg(pg, monkeypatch) as client:
        r = client.post("/api/v1/sessions", json={"is_sandbox": True})
        assert r.status_code == 200
        sid = r.json()["session_id"]
    assert len(pg.sessions) == 1
    assert pg.sessions[0][0] == sid


@pytest.mark.asyncio
async def test_ingestion_persistence_failure_log_excludes_comment_content(caplog):
    class _BrokenPgStore:
        enabled = True

        async def insert_viewer_msg(self, *args, **kwargs):
            raise RuntimeError("database failure")

    comment = "private-comment"
    store = InMemorySessionStore()
    await store.set("session-safe", {"status": "active"})
    service = PlatformEventIngestionService(
        store=store,
        pg_store=_BrokenPgStore(),
    )
    with caplog.at_level(logging.WARNING, logger="backend.application.platform_events"):
        await service.ingest(
            "session-safe",
            [PlatformEvent(**{**_event(comment, "evt-1"), "occurred_at": time.time()})],
        )

    assert "session-safe" in caplog.text
    assert "operation=insert_viewer_msg" in caplog.text
    assert comment not in caplog.text


def test_lite_ingest_no_pg_behaves_unchanged(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    deps = _Deps(
        backend=config.build_render_backend(),
        store=config.build_store(),
        director=None,
        engine_manager=None,
        config=config,
        locks=None,
        orchestrators={},
        coordinator=None,
        pg_store=None,
        event_ingestion=PlatformEventIngestionService(store=config.build_store()),
    )
    with TestClient(create_app(config=config, deps=deps)) as client:
        r = client.post(
            "/api/v1/sessions/x/events",
            json={"events": [_event("hi", "evt-2")]},
        )
        # Unknown session with no pg -> 404, and no persistence crash.
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_persists_rejected_audit_event_with_sanitized_reason():
    class _RecordingPg:
        enabled = True
        audit_events = []

        async def insert_audit_event(self, event_type, **kw):
            _RecordingPg.audit_events.append((event_type, kw))
            return len(_RecordingPg.audit_events)

    store = InMemorySessionStore()
    await store.set("session-safe", {"status": "active"})
    service = PlatformEventIngestionService(
        store=store,
        pg_store=_RecordingPg(),
    )
    stale = {**_event("x", "evt-stale"), "occurred_at": time.time() - 3600 * 24 * 2}
    result = await service.ingest("session-safe", [PlatformEvent(**stale)])

    assert result["events"][0]["status"] == "rejected"
    assert result["events"][0]["reason"] == "occurred_at_out_of_range"
    assert _RecordingPg.audit_events[0][0] == "event_ingress.rejected"
    detail = _RecordingPg.audit_events[0][1]["detail"]
    assert detail["reason"] == "occurred_at_out_of_range"
    assert "x" not in str(detail)  # raw viewer text never lands in the audit row


from backend.application.platform_events import PlatformEvent  # noqa: E402
from conftest import make_deps as _Deps  # noqa: E402, F401
