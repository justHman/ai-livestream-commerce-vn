"""API contract tests for /api/v1/entities (Data Studio, tasks 9.1-9.8).

Covers PUT create/update with revision semantics, 409 on concurrent stale
writes, GET list/filter, 404s, DELETE, the exact render-preview output, and
the suggestion seam returning empty suggestions when no LLM is configured
(the save path never depends on extraction).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from backend.application.entity.models import EntityDocument
from backend.application.entity.registry import COMMERCE_PRICE_CURRENT

from conftest import make_deps as _Deps  # noqa: F401

_TOKEN = "viewer" + "-secret"


def _client(app_env: str = "dev") -> TestClient:
    from backend.application.render.mock import MockRenderBackend
    from backend.config import AppConfig
    from backend.main import create_app

    config = AppConfig(
        render_backend="mock",
        app_env=app_env,
        backend_api_token=_TOKEN if app_env == "prod" else "",
        cors_origins="https://example.com" if app_env == "prod" else "*",
    )
    deps = _Deps(backend=MockRenderBackend(), config=config)
    return TestClient(create_app(config=config, deps=deps))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _form(**overrides: Any) -> dict[str, Any]:
    body = {
        "id": "p1",
        "entity_type": "product",
        "name": "SP 1",
        "aliases": [],
        "tags": [],
        "common": {},
        "fact_rows": [{"label": "Giá hiện tại", "value": "350000", "unit": None}],
        "knowledge_blocks": [{"kind": "description", "title": "Mô tả", "content": "Kem ABC."}],
    }
    body.update(overrides)
    return body


def test_put_creates_entity_revision_1() -> None:
    with _client() as client:
        resp = client.put("/api/v1/entities/p1", json=_form())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "p1"
    assert body["revision"] == 1


def test_put_create_converts_rows_into_typed_facts() -> None:
    with _client() as client:
        body = client.put("/api/v1/entities/p1", json=_form()).json()

    assert body["facts"][0]["key"] == "commerce.price.current"
    assert body["facts"][0]["type"] == "int"


def test_put_update_bumps_revision_to_2() -> None:
    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form())
        resp = client.put(
            "/api/v1/entities/p1",
            json=_form(
                name="SP 1 mới",
                fact_rows=[{"label": "Giá hiện tại", "value": "400000", "unit": None}],
            ),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    assert resp.json()["name"] == "SP 1 mới"


def test_put_path_body_id_mismatch_400() -> None:
    with _client() as client:
        resp = client.put("/api/v1/entities/p1", json=_form(id="p2"))

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "entity_id_mismatch"


def test_put_unknown_common_key_400() -> None:
    with _client() as client:
        resp = client.put(
            "/api/v1/entities/p1",
            json=_form(common={"custom.not-allowed": "x"}),
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unknown_common_key"


def test_put_stale_revision_conflict_409() -> None:
    """The repository's revision guard turns a concurrent write into 409.

    Simulated with a racing repo: a concurrent writer lands rev+1 BETWEEN
    the handler's ``get`` and its ``upsert`` (the only genuine race — the
    handler always builds from the freshly read stored revision).
    """
    from backend.application.entity.repository import InMemoryEntityRepository

    class RacingRepo(InMemoryEntityRepository):
        async def get(self, entity_id: str):
            entity = await super().get(entity_id)
            if entity is not None:
                await super().upsert(entity.model_copy(update={"revision": entity.revision + 1}))
            return entity

    from backend.application.render.mock import MockRenderBackend
    from backend.config import AppConfig
    from backend.main import create_app

    config = AppConfig(render_backend="mock", app_env="dev")
    deps = _Deps(backend=MockRenderBackend(), config=config, entity_repo=RacingRepo())
    with TestClient(create_app(config=config, deps=deps)) as client:
        client.put("/api/v1/entities/p1", json=_form())
        stale = client.put("/api/v1/entities/p1", json=_form(name="SP 1 stale"))

    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "revision_conflict"


def test_get_returns_stored_entity() -> None:
    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form())
        resp = client.get("/api/v1/entities/p1")

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "SP 1"


def test_get_missing_entity_404() -> None:
    with _client() as client:
        resp = client.get("/api/v1/entities/nope")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "entity_not_found"


def test_list_returns_entities_sorted_by_id() -> None:
    with _client() as client:
        client.put("/api/v1/entities/b", json=_form(id="b", name="SP B"))
        client.put("/api/v1/entities/a", json=_form(id="a", name="SP A"))
        resp = client.get("/api/v1/entities")

    assert resp.status_code == 200, resp.text
    assert [e["id"] for e in resp.json()["entities"]] == ["a", "b"]


def test_list_filters_by_entity_type() -> None:
    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form(id="p1"))
        client.put(
            "/api/v1/entities/s1",
            json=_form(id="s1", entity_type="shop", name="Shop 1"),
        )
        resp = client.get("/api/v1/entities", params={"entity_type": "shop"})

    assert resp.status_code == 200, resp.text
    assert [e["id"] for e in resp.json()["entities"]] == ["s1"]


def test_delete_returns_204_then_404() -> None:
    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form())
        deleted = client.delete("/api/v1/entities/p1")
        missing = client.delete("/api/v1/entities/p1")

    assert deleted.status_code == 204, deleted.text
    assert missing.status_code == 404, missing.text


def test_render_preview_matches_render_entity_context() -> None:
    from backend.application.entity.render import render_entity_context

    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form())
        resp = client.post(
            "/api/v1/entities/p1/render-preview",
            json={"selectors": ["Giá hiện tại"], "max_block_chars": 400},
        )
        stored = EntityDocument.model_validate(client.get("/api/v1/entities/p1").json())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity_id"] == "p1"
    assert body["selectors"] == ["Giá hiện tại"]
    assert body["rendered"] == render_entity_context(
        stored, selectors=["Giá hiện tại"], max_block_chars=400
    )


def test_render_preview_missing_entity_404() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/entities/nope/render-preview",
            json={"selectors": [], "max_block_chars": 400},
        )

    assert resp.status_code == 404, resp.text


def test_render_preview_selector_filters_facts() -> None:
    with _client() as client:
        client.put("/api/v1/entities/p1", json=_form())
        resp = client.post(
            "/api/v1/entities/p1/render-preview",
            json={"selectors": ["Giá hiện tại"]},
        )

    assert resp.status_code == 200, resp.text
    rendered = resp.json()["rendered"]
    assert COMMERCE_PRICE_CURRENT in rendered
    assert "identity.brand" not in rendered


def test_suggestions_empty_without_llm() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/entities/suggestions",
            json={"entity_type": "product", "text": "Kem ABC giá 100k.", "block_kind": "custom"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggestions"] == []
    assert body["note"] is None


def test_suggestions_validation_422_on_empty_text() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/entities/suggestions",
            json={"entity_type": "product", "text": ""},
        )

    assert resp.status_code == 422, resp.text


def test_entities_require_viewer_auth_in_prod() -> None:
    with _client(app_env="prod") as client:
        resp = client.get("/api/v1/entities")

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "http_401"
