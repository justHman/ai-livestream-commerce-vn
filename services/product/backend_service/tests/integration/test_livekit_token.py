"""Offline tests for LiveKit room token mint (Task 13)."""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig
from backend.application.publishing.livekit import LiveKitConfigError, mint_room_token
from conftest import make_deps as _Deps  # noqa: F401


API_KEY = "devkey"
API_SECRET = "devsecret-devsecret-devsecret-32b"
FIXED_NOW = 1_700_000_000


def _deps(cfg: AppConfig):
    return _Deps(config=cfg)


def test_mint_room_token_claims():
    token = mint_room_token(
        api_key=API_KEY,
        api_secret=API_SECRET,
        room="sess-abc",
        identity="viewer-1",
        ttl_sec=120,
        now=FIXED_NOW,
    )
    claims = jwt.decode(
        token,
        API_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["iss"] == API_KEY
    assert claims["sub"] == "viewer-1"
    assert claims["nbf"] == FIXED_NOW
    assert claims["exp"] == FIXED_NOW + 120
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["room"] == "sess-abc"
    # Canonical default: mint_room_token grants publish (publisher tokens);
    # viewer subscribe-only tokens pass can_publish=False explicitly.
    assert claims["video"]["canPublish"] is True


def test_mint_subscribe_only_token_room_is_session_id():
    token = mint_room_token(
        api_key=API_KEY,
        api_secret=API_SECRET,
        room="sid-42",
        identity="viewer-sid-42",
        name="viewer-sid-42",
        can_publish=False,
        can_subscribe=True,
        now=FIXED_NOW,
    )
    claims = jwt.decode(
        token,
        API_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["video"]["room"] == "sid-42"
    assert claims["sub"] == "viewer-sid-42"


def test_mint_requires_secret():
    with pytest.raises(LiveKitConfigError):
        mint_room_token(
            api_key=API_KEY,
            api_secret="",
            room="r",
            identity="u",
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)

    cfg = AppConfig.from_env()
    from backend.main import create_app

    app = create_app(config=cfg, deps=_deps(cfg))
    return TestClient(app)


def test_endpoint_returns_token_and_room(client: TestClient):
    resp = client.post("/api/v1/media/livekit/room/sess-xyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["livekit_url"] == "ws://livekit:7880"
    assert body["room"] == "sess-xyz"
    claims = jwt.decode(
        body["token"],
        API_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["video"]["room"] == "sess-xyz"
    assert claims["video"]["roomJoin"] is True
    assert claims["iss"] == API_KEY


def test_endpoint_503_when_secret_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    cfg = AppConfig.from_env()
    assert cfg.livekit_api_secret == ""
    from backend.main import create_app

    app = create_app(config=cfg, deps=_deps(cfg))
    with TestClient(app) as c:
        resp = c.post("/api/v1/media/livekit/room/s1")
        assert resp.status_code == 503
        assert "LiveKit" in resp.json()["error"]["message"]
