"""Editable shop profile and ordered product attachment contracts."""

from __future__ import annotations

from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.director.catalog import Product
from core.director.embedder import HashingEmbedder
from core.director.runtime import DirectorRuntime
from core.engine_manager import EngineManager
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


def _client(backend: MockRenderBackend | None = None) -> TestClient:
    from core.server import create_app

    backend = backend or MockRenderBackend()
    runtime = DirectorRuntime(backend, embedder=HashingEmbedder())
    config = AppConfig(render_backend="mock", app_env="dev", director_enabled=True)
    dependencies = v1.V1Deps(
        backend=backend,
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        director=runtime,
        engine_manager=EngineManager(),
        config=config,
    )
    return TestClient(create_app(config=config, deps=dependencies))


def _product(product_id: str, name: str, price: int = 100) -> dict:
    return {
        "id": product_id,
        "name": name,
        "description": name,
        "price": price,
        "original_price": price + 20,
        "stock_total": 10,
    }


def test_attach_preserves_operator_product_order_and_structured_shop_profile() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
                "shop_profile": {
                    "shop_name": "Livento",
                    "host_name": "Chị Lan",
                    "address": "TP.HCM",
                    "phone": "0900000000",
                    "selling_style": "nhiệt tình",
                },
                "products": [
                    _product("P004", "Áo hoodie HeyGen"),
                    _product("P001", "Kem chống nắng"),
                ],
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["products"] == ["P004", "P001"]
        assert response.json()["will_speak"] is False
        assert response.json()["profile_revision"] == 1
        assert response.json()["catalog_revision"] == 1
        state = v1.deps().director.get_session(session_id).director.state
        assert [product.product_id for product in state.products] == ["P004", "P001"]
        assert state.current_product().product_id == "P004"


def test_attach_serializes_structured_shop_profile_to_persona() -> None:
    class PersonaBackend(MockRenderBackend):
        def __init__(self) -> None:
            super().__init__()
            self.personas: dict[str, str] = {}

        def set_persona(self, session_id: str, shop_profile: str) -> None:
            self.personas[session_id] = shop_profile

    backend = PersonaBackend()
    with _client(backend) as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
                "shop_profile": {
                    "shop_name": "Livento",
                    "host_name": "Chị Lan",
                    "address": "TP.HCM",
                    "phone": "0900000000",
                    "selling_style": "nhiệt tình",
                },
                "products": [_product("P004", "Áo hoodie HeyGen")],
            },
        )

    assert response.status_code == 200
    assert backend.personas[session_id] == (
        "Tên shop: Livento\nTên MC: Chị Lan\nĐịa chỉ: TP.HCM\n"
        "Điện thoại: 0900000000\nPhong cách bán hàng: nhiệt tình"
    )


def test_reattach_updates_remaining_opening_without_restarting_it() -> None:
    backend = MockRenderBackend()
    runtime = DirectorRuntime(backend, embedder=HashingEmbedder())
    session_id = "reattach-opening"
    runtime.attach(session_id, [Product(id="P004", name="Áo hoodie")])
    session = runtime.get_session(session_id)
    first = session.director.decide([], now=0.0)
    session.director.mark_spoken(first)
    second = session.director.decide([], now=1.0)
    session.director.mark_spoken(second)

    runtime.attach(session_id, [Product(id="P001", name="Kem chống nắng")])
    third = session.director.decide([], now=2.0)

    assert third.task_id == "opening:3"
    assert "Kem chống nắng" in third.text
    assert "Áo hoodie" not in third.text


def test_reattach_increments_revisions_and_preserves_current_checkpoint() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        first = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie")]},
        ).json()
        runtime_session = v1.deps().director.get_session(session_id)
        runtime_session.director.state.products[0].is_introduced = True
        second = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie updated")]},
        ).json()

    assert second["profile_revision"] == first["profile_revision"]
    assert second["catalog_revision"] == first["catalog_revision"] + 1
    assert second["generation_token"] == "1:2:0"
    assert (
        v1.deps().director.get_session(session_id).director.state.products[0].is_introduced is True
    )


def test_reattach_replaces_catalog_without_leaving_stale_coordinator() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        first = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie")]},
        )
        second = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P001", "Kem chống nắng")]},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    state = v1.deps().director.get_session(session_id).director.state
    assert [product.product_id for product in state.products] == ["P001"]


def test_attach_applies_runtime_config_atomically() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
                "products": [_product("P004", "Áo hoodie")],
                "runtime_config": {
                    "prepared_turn_depth": 4,
                    "max_qa_clusters_per_window": 3,
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["config_revision"] == 1
    session = v1.deps().director.get_session(session_id)
    assert session.director.cfg.prepared_turn_depth == 4
    assert session.accepted_snapshot["runtime_config"] == {
        "max_qa_clusters_per_window": 3,
        "prepared_turn_depth": 4,
    }


def test_attach_rejects_invalid_runtime_config_without_mutating_existing_session() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        first = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie")]},
        ).json()
        invalid = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
                "products": [_product("P001", "Kem chống nắng")],
                "runtime_config": {
                    "demand_pivot_enter_share": 0.4,
                    "demand_pivot_exit_share": 0.8,
                },
            },
        )

    session = v1.deps().director.get_session(session_id)
    assert invalid.status_code == 422
    assert session.generation_token == first["generation_token"]
    assert [product.id for product in session.catalog] == ["P004"]


def test_runtime_config_update_applies_revision_and_rejects_invalid_rate() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie")]},
        )
        response = client.patch(
            "/api/v1/lite/config", json={"session_id": session_id, "prepared_turn_depth": 4}
        )
        invalid = client.patch(
            "/api/v1/lite/config", json={"session_id": session_id, "comment_rate": 9}
        )

    assert response.status_code == 200
    assert response.json()["config_revision"] == 1
    assert invalid.status_code == 422


def test_runtime_config_update_requires_attached_session() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.patch(
            "/api/v1/lite/config", json={"session_id": session_id, "prepared_turn_depth": 4}
        )

    assert response.status_code == 409


def test_runtime_config_update_preserves_previous_revision_after_validation_failure() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [_product("P004", "Áo hoodie")]},
        )
        invalid = client.patch(
            "/api/v1/lite/config", json={"session_id": session_id, "demand_pivot_exit_share": 0.8}
        )

    assert invalid.status_code == 422
    assert v1.deps().director.get_session(session_id).config_revision == 0


def test_attach_rejects_duplicate_product_ids() -> None:
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={
                "session_id": session_id,
                "products": [_product("P004", "A"), _product("P004", "B")],
            },
        )

    assert response.status_code == 422


def test_attach_rejects_invalid_price_relationship_and_stock() -> None:
    invalid = _product("P004", "Áo hoodie", price=200)
    invalid["original_price"] = 100
    invalid["stock_total"] = -1
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [invalid]},
        )

    assert response.status_code == 422


def test_attach_rejects_oversized_product_arrays() -> None:
    invalid = _product("P004", "Áo hoodie")
    invalid["colors"] = [f"Màu {index}" for index in range(33)]
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [invalid]},
        )

    assert response.status_code == 422


def test_attach_rejects_catalog_over_limit() -> None:
    products = [_product(f"P{index:03}", f"Sản phẩm {index}") for index in range(101)]
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": products},
        )

    assert response.status_code == 422


def test_attach_rejects_oversized_array_item() -> None:
    invalid = _product("P004", "Áo hoodie")
    invalid["features"] = ["x" * 501]
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [invalid]},
        )

    assert response.status_code == 413


def test_attach_rejects_oversized_image_reference() -> None:
    invalid = _product("P004", "Áo hoodie")
    invalid["ref_image"] = "https://example.com/" + "x" * 2_048
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [invalid]},
        )

    assert response.status_code == 413


def test_attach_validation_identifies_affected_product_field() -> None:
    invalid = _product("P004", "Áo hoodie", price=200)
    invalid["original_price"] = 100
    with _client() as client:
        session_id = client.post("/api/v1/lite/start", json={}).json()["session_id"]
        response = client.post(
            "/api/v1/lite/attach",
            json={"session_id": session_id, "products": [invalid]},
        )

    location = response.json()["detail"][0]["loc"]
    assert location == ["body", "products", 0]
