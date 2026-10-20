"""Sessions aliases + admin + mock gate (M3). Offline mock backend."""

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


def _deps():
    return _Deps(
        config=AppConfig(render_backend="mock", app_env="dev"),
    )


def _client(cfg: AppConfig | None = None) -> TestClient:
    from backend.main import create_app

    cfg = cfg or AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    d = _deps()
    d.config = cfg
    app = create_app(config=cfg, deps=d)
    return TestClient(app)


def _client_with_backend(cfg: AppConfig, backend) -> TestClient:
    from backend.main import create_app

    d = _deps()
    d.config = cfg
    d.backend = backend
    return TestClient(create_app(config=cfg, deps=d))


def test_sessions_start_and_stop_alias(mock_env: None) -> None:
    with _client() as client:
        r = client.post("/api/v1/sessions", json={})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        # canonical say path
        r2 = client.post(f"/api/v1/sessions/{sid}/say", json={"text": "xin chào"})
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["code"] == "missing_binding"
        r3 = client.post(f"/api/v1/sessions/{sid}/say", json={"text": "deal hot"})
        assert r3.status_code == 409, r3.text
        r4 = client.post(f"/api/v1/sessions/{sid}/stop")
        assert r4.status_code == 200, r4.text
        assert r4.json()["stopped"] == sid


def test_session_avatar_overrides_require_internal_auth_and_reach_backend(mock_env: None) -> None:
    from backend.application.render.engines_base import StartResult

    class CaptureBackend:
        name = "capture"

        def start(self, opts):
            self.opts = opts
            return StartResult("capture-1", "ws://livekit", "token", "CAPTURE")

    payload = {
        "avatar_provider": "lemonslice",
        "avatar_agent_id": "agent_093d2f9535db94ef",
        "voice_id": "Hai Dang",
    }
    backend = CaptureBackend()
    with _client_with_backend(AppConfig(render_backend="mock", app_env="dev"), backend) as client:
        denied = client.post("/api/v1/sessions", json=payload)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "avatar_override_not_authorized"

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-test-token",
        avatar_internal_token="internal-test-token",
        avatar_image_hosts="avatars.example.test",
    )
    with _client_with_backend(cfg, backend) as client:
        viewer_only = client.post(
            "/api/v1/sessions",
            json=payload,
            headers={"Authorization": "Bearer viewer-test-token"},
        )
        wrong_internal = client.post(
            "/api/v1/sessions",
            json=payload,
            headers={
                "Authorization": "Bearer viewer-test-token",
                "X-Livento-Internal-Token": "nope",
            },
        )
        started = client.post(
            "/api/v1/sessions",
            json=payload,
            headers={
                "Authorization": "Bearer viewer-test-token",
                "X-Livento-Internal-Token": "internal-test-token",
            },
        )
    assert started.json().get("avatar_override_applied") is True
    assert viewer_only.status_code == 403, (
        "the viewer token alone must not choose an avatar or voice"
    )
    assert wrong_internal.status_code == 403
    assert started.status_code == 200, started.text
    assert backend.opts.avatar_provider == "lemonslice"
    assert backend.opts.avatar_agent_id == "agent_093d2f9535db94ef"
    assert backend.opts.voice_id == "Hai Dang"


@pytest.mark.parametrize(
    "payload",
    [
        {"avatar_agent_id": "agent_uppercase"},
        {"avatar_image_url": "http://avatars.example.test/a.png"},
        {
            "avatar_agent_id": "agent_093d2f9535db94ef",
            "avatar_image_url": "https://avatars.example.test/a.png",
        },
        {"voice_id": "   "},
        {"avatar_image_url": "https://avatars.example.test:8443/a.png"},
        {"avatar_image_url": "https://user:pw@avatars.example.test/a.png"},
        {"avatar_image_url": "https://192.168.0.10/a.png"},
        {"avatar_image_url": "https://avatars.example.test/a/../b.png"},
        {"avatar_image_url": "https://avatars.example.test/a%2f..%2fb.png"},
        {"avatar_image_url": "https://avatars.example.test/a.png#x"},
    ],
)
def test_session_avatar_override_shape_validation(mock_env: None, payload: dict) -> None:
    with _client() as client:
        response = client.post("/api/v1/sessions", json=payload)
    assert response.status_code == 422


def test_session_avatar_image_host_must_be_allowlisted(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-test-token",
        avatar_internal_token="internal-test-token",
        avatar_image_hosts="avatars.example.test",
    )
    with _client(cfg) as client:
        response = client.post(
            "/api/v1/sessions",
            json={
                "avatar_provider": "lemonslice",
                "avatar_image_url": "https://outside.example.test/avatar.png",
                "voice_id": "Hai Dang",
            },
            headers={
                "Authorization": "Bearer viewer-test-token",
                "X-Livento-Internal-Token": "internal-test-token",
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "avatar_image_host_not_allowed"


def test_plan_create_stores_run_plan(mock_env: None) -> None:
    with _client() as client:
        sid = client.post("/api/v1/sessions", json={}).json()["session_id"]
        body = {
            "persona": "MC demo",
            "products": [
                {
                    "id": "p1",
                    "name": "Áo thun",
                    "features": ["vải cotton", "sale 50%"],
                    "price": 99000,
                }
            ],
        }
        r = client.post(f"/api/v1/sessions/{sid}/plan/create", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        plan = data["plan"]
        assert "opening" in plan["phases"]
        assert "selling" in plan["phases"]
        assert "closing" in plan["phases"]
        assert plan["selling"][0]["product_id"] == "p1"
        assert "vải cotton" in plan["selling"][0]["key_selling_points"]


def test_admin_config_no_secret_values(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-secret",
        admin_api_token="admin-secret",
    )
    admin = {"Authorization": "Bearer admin-secret"}
    with _client(cfg) as client:
        r = client.get("/api/v1/admin/config", headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["secrets"]["backend_api_token"] == "present"
        assert "viewer-secret" not in r.text
        assert "admin-secret" not in r.text
        r2 = client.get("/api/v1/admin/health", headers=admin)
        assert r2.status_code == 200
        assert "status" in r2.json()


def test_mock_routes_404_in_prod_without_debug(mock_env: None) -> None:
    cfg = AppConfig(
        render_backend="mock",
        app_env="prod",
        debug_enabled=False,
        cors_origins="https://example.com",
        backend_api_token="",
        admin_api_token="",
    )
    # create_app may reject prod CORS * — we set explicit origin.
    with _client(cfg) as client:
        r = client.get("/api/v1/mock/status/nope")
        assert r.status_code == 404


def test_overrides_are_refused_when_the_internal_token_is_not_configured(mock_env: None) -> None:
    cfg = AppConfig(render_backend="mock", app_env="dev", backend_api_token="viewer-test-token")
    with _client(cfg) as client:
        response = client.post(
            "/api/v1/sessions",
            json={
                "avatar_provider": "lemonslice",
                "avatar_agent_id": "agent_093d2f9535db94ef",
                "voice_id": "Hai Dang",
            },
            headers={
                "Authorization": "Bearer viewer-test-token",
                "X-Livento-Internal-Token": "anything",
            },
        )
    assert response.status_code == 403


def test_incomplete_override_bundles_are_rejected_by_the_request_model() -> None:
    from backend.api.v1.router import StartReq

    for body in (
        {"avatar_agent_id": "agent_093d2f9535db94ef"},
        {"avatar_provider": "lemonslice", "avatar_agent_id": "agent_093d2f9535db94ef"},
        {"avatar_provider": "lemonslice", "voice_id": "Hai Dang"},
        {"voice_id": "Hai Dang"},
        {"avatar_provider": "lemonslice"},
    ):
        with pytest.raises(ValueError):
            StartReq(**body)
    StartReq(
        avatar_provider="lemonslice", avatar_agent_id="agent_093d2f9535db94ef", voice_id="Hai Dang"
    )
