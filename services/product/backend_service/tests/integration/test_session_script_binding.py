"""Task 12.2: PUT /api/v1/sessions/{session_id}/script-set API contract.

Offline integration test (mock backend, in-memory session store, fake
authoring source injected into the container). Covers:

- 404 unknown session;
- 409 structured missing/stale details when scripts are not ready
  (Decision 16);
- 200 ok + binding snapshot persisted in session state (task 12.3);
- no authoring artifact mutation: the fake source's ScriptSet/version
  rows are untouched after binding.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


SET_ID = "script_set:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _FakeSource:
    """In-memory authoring source satisfying the BindingSource protocol.

    Stores plain dicts in the wire shape the API layer passes through.
    """

    def __init__(self) -> None:
        self.script_set = {
            "id": SET_ID,
            "shop_id": "shop-1",
            "product_ids": ["P001", "P002"],
            "brief": {"transition_policy": "ORDER_AGNOSTIC"},
        }
        # product_id -> item dict
        self.items = {
            "P001": {
                "id": "script_item:11111111111111111111111111111111",
                "script_set_id": SET_ID,
                "product_id": "P001",
                "state": "approved",
                "approved_version_id": "script_version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            "P002": {
                "id": "script_item:22222222222222222222222222222222",
                "script_set_id": SET_ID,
                "product_id": "P002",
                "state": "approved",
                "approved_version_id": "script_version:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        }
        # item_id -> version dict
        self.versions = {
            "script_item:11111111111111111111111111111111": {
                "id": "script_version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "script_item_id": "script_item:11111111111111111111111111111111",
                "version": 1,
                "state": "approved",
                "display_text": "Kem chống nắng SPF50",
                "spoken_text": "Kem chống nắng SPF50, 350.000 đồng.",
            },
            "script_item:22222222222222222222222222222222": {
                "id": "script_version:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "script_item_id": "script_item:22222222222222222222222222222222",
                "version": 1,
                "state": "approved",
                "display_text": "Serum Vitamin C",
                "spoken_text": "Serum Vitamin C làm sáng da, 250.000 đồng.",
            },
        }
        # item_id -> approval dict
        self.approvals = {
            "script_item:11111111111111111111111111111111": {
                "id": "approval:11111111111111111111111111111111",
                "script_item_id": "script_item:11111111111111111111111111111111",
                "script_version_id": "script_version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "actor": "admin",
                "approval_hash": "0" * 64,
                "gate_run_id": "gate_run:11111111111111111111111111111111",
            },
            "script_item:22222222222222222222222222222222": {
                "id": "approval:22222222222222222222222222222222",
                "script_item_id": "script_item:22222222222222222222222222222222",
                "script_version_id": "script_version:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "actor": "admin",
                "approval_hash": "0" * 64,
                "gate_run_id": "gate_run:22222222222222222222222222222222",
            },
        }

    async def get_script_set(self, *, set_id: str) -> dict | None:
        return self.script_set if set_id == SET_ID else None

    async def get_script_item(self, *, set_id: str, product_id: str) -> dict | None:
        return self.items.get(product_id)

    async def get_script_version(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict | None:
        item = self.items.get(product_id)
        if item is None:
            return None
        return self.versions.get(item["id"])

    async def get_approval(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict | None:
        item = self.items.get(product_id)
        if item is None:
            return None
        return self.approvals.get(item["id"])

    def current_dependencies(self):
        from backend.application.script_authoring.session_binding import DependencyFingerprint

        return getattr(self, "current_deps", None) or DependencyFingerprint()

    def get_approved_version(self, *, script_set_id: str, product_id: str) -> dict | None:
        """ApprovedScriptStore protocol: exact version + spoken_text."""
        from backend.application.script_authoring.runtime_handoff import (
            ResolvedApprovedScript,
        )

        item = self.items.get(product_id)
        if item is None:
            return None
        version = self.versions.get(item["id"])
        if version is None:
            return None
        return ResolvedApprovedScript(
            product_id=product_id,
            approved_version_id=version["id"],
            spoken_text=version["spoken_text"],
        )


class _FakeDirectorRuntime:
    """Minimal runtime catalog fake matching ``RuntimeCatalogProxy``'s read.

    The proxy reads ``runtime._sessions[..].catalog`` for product ids —
    exactly the shape a real attached Director session exposes. ``detach``
    is a no-op so the session-stop route works in tests.
    """

    def __init__(self, product_ids: set[str]) -> None:
        # Task 8.7: the runtime catalog holds EntityDocument values; the
        # binding proxy reads the entity ``id`` as the product id.
        session = type(
            "S", (), {"catalog": [type("P", (), {"id": pid})() for pid in product_ids]}
        )()
        self._sessions = {"sess": session}

    def detach(self, session_id: str) -> None:  # noqa: ARG002
        self._sessions.pop(session_id, None)


def _make_app(source: _FakeSource, *, director: object | None = None):
    """Build an app with the fake source injected into the container."""
    from backend.main import create_app

    d = _Deps(
        config=AppConfig(render_backend="mock", app_env="dev"),
        director=director,
    )
    app = create_app(config=d.config, deps=d)
    app.state.container.script_authoring_service = source
    return app


def _start_session(client: TestClient) -> str:
    r = client.post("/api/v1/sessions", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_bind_unknown_session_404(mock_env: None) -> None:
    with TestClient(_make_app(_FakeSource())) as client:
        r = client.put(
            "/api/v1/sessions/nope/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "http_404"


def test_bind_unknown_script_set_409(mock_env: None) -> None:
    with TestClient(_make_app(_FakeSource())) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": "script_set:99999999999999999999999999999999"},
        )
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "missing_or_stale_script"
        assert error["details"]["issues"][0]["kind"] == "unknown_set"


def test_bind_missing_script_returns_structured_409(mock_env: None) -> None:
    source = _FakeSource()
    source.items = {}  # no script items at all
    with TestClient(_make_app(source)) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "missing_or_stale_script"
        assert error["details"]["ok"] is False
        assert sorted(i["product_id"] for i in error["details"]["missing"]) == ["P001", "P002"]


def test_bind_stale_entity_facts_revision_returns_409(mock_env: None) -> None:
    """Task 8.9: fact revision change -> different version -> STALE.

    The entity-derived versions feed ``DependencyFingerprint``; the
    recorded-at-approval fingerprint differs from current -> 409 stale.
    """
    from backend.application.entity.fingerprints import entity_facts_version
    from backend.application.entity.models import EntityDocument, Fact
    from backend.application.script_authoring.session_binding import DependencyFingerprint

    def make_entity(price_revision: int) -> EntityDocument:
        return EntityDocument(
            id="product:entity-p1",
            entity_type="product",
            revision=1,
            name="Kem ABC",
            facts=[
                Fact(
                    key="commerce.price.current",
                    type="int",
                    value=350000,
                    unit="VND",
                    revision=price_revision,
                    freshness="volatile",
                    updated_at="2026-08-01T00:00:00+00:00",
                ),
            ],
        )

    recorded = DependencyFingerprint(
        product_facts_version=entity_facts_version(make_entity(1)),
    )
    source = _FakeSource()
    source.recorded_dependencies_by_item = {
        "script_item:11111111111111111111111111111111": recorded,
        "script_item:22222222222222222222222222222222": recorded,
    }
    # A price fact revision bump after approval must stale the binding.
    source.current_deps = DependencyFingerprint(
        product_facts_version=entity_facts_version(make_entity(2)),
    )
    with TestClient(_make_app(source)) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "missing_or_stale_script"
        assert sorted(i["product_id"] for i in error["details"]["stale"]) == ["P001", "P002"]


def test_bind_stale_approval_returns_structured_409(mock_env: None) -> None:
    source = _FakeSource()
    # Approvals recorded rules-1 but current deps are rules-2 -> STALE.
    from backend.application.script_authoring.session_binding import DependencyFingerprint

    source.recorded_dependencies_by_item = {
        "script_item:11111111111111111111111111111111": DependencyFingerprint(
            rule_set_version="rules-1"
        ),
        "script_item:22222222222222222222222222222222": DependencyFingerprint(
            rule_set_version="rules-1"
        ),
    }
    source.current_deps = DependencyFingerprint(rule_set_version="rules-2")
    with TestClient(_make_app(source)) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "missing_or_stale_script"
        assert error["details"]["ok"] is False
        assert sorted(i["product_id"] for i in error["details"]["stale"]) == ["P001", "P002"]


def test_bind_ok_persists_snapshot_without_mutating_authoring(mock_env: None) -> None:
    source = _FakeSource()
    director = _FakeDirectorRuntime({"P001", "P002"})
    with TestClient(_make_app(source, director=director)) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["binding"]["script_set_id"] == SET_ID
        products = body["binding"]["products"]
        assert {p["product_id"] for p in products} == {"P001", "P002"}
        assert products[0]["approved_version_id"].startswith("script_version:")
        assert "spoken_text" in products[0]

        # Snapshot persisted in session state (task 12.3).
        meta = client.post(f"/api/v1/sessions/{sid}/stop")
        assert meta.status_code == 200, meta.text

        # Authoring artifacts untouched: the fake source still has its rows.
        assert source.script_set["id"] == SET_ID
        assert source.versions["script_item:11111111111111111111111111111111"]["id"] == (
            "script_version:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )


def test_bind_ok_snapshot_in_session_store(mock_env: None) -> None:
    """The binding snapshot is stored under session metadata, not authoring rows."""
    import asyncio

    source = _FakeSource()
    director = _FakeDirectorRuntime({"P001", "P002"})
    with TestClient(_make_app(source, director=director)) as client:
        sid = _start_session(client)
        r = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            json={"script_set_id": SET_ID},
        )
        assert r.status_code == 200, r.text
        # Read the session store directly: metadata carries the binding.
        store = client.app.state.container.store
        meta = asyncio.run(store.get(sid))
        assert meta is not None
        binding = meta.get("script_set_binding")
        assert binding is not None
        assert binding["script_set_id"] == SET_ID
