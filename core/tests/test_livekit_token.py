"""Offline tests for LiveKit room token mint (Task 13)."""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.livekit_tokens import (
    LiveKitConfigError,
    mint_room_token,
    mint_session_viewer_token,
)
from core.render.mock import MockRenderBackend
from core.store import InMemorySessionStore


API_KEY = "devkey"
API_SECRET = "devsecret-devsecret-devsecret-32b"
FIXED_NOW = 1_700_000_000


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
    assert claims["video"]["canSubscribe"] is True
    assert claims["video"]["canPublish"] is False


def test_mint_session_viewer_token_room_is_session_id():
    token = mint_session_viewer_token(
        api_key=API_KEY,
        api_secret=API_SECRET,
        session_id="sid-42",
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
    deps = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        config=cfg,
    )
    v1.init_deps(deps)
    from core.server import create_app

    app = create_app(deps=deps)
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
    deps = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        config=cfg,
    )
    v1.init_deps(deps)
    from core.server import create_app

    app = create_app(deps=deps)
    with TestClient(app) as c:
        resp = c.post("/api/v1/media/livekit/room/s1")
        assert resp.status_code == 503
        assert "LiveKit" in resp.json()["detail"]
