"""GET /api/v1/tts/voices: auth, availability and caching. The TTS server is a fake httpx transport."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.v1 import tts_voices
from backend.application.clients.tts import self_hosted
from backend.config import AppConfig, TTSConfig
from conftest import make_deps as _Deps

URL = "/api/v1/tts/voices"
VIEWER = {"Authorization": "Bearer viewer-test-token"}
AUTH = {**VIEWER, "X-Livento-Internal-Token": "internal-test-token"}
DOWN = {"voices": [], "available": False}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_voices, "_cache", None)
    monkeypatch.setenv("SESSION_STORE", "memory")


class FakeTTS:
    """Swaps the TTS client's HTTP transport for a handler and counts upstream calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler) -> None:
        self.calls = 0
        real = httpx.Client

        def counting(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return handler(request)

        monkeypatch.setattr(
            self_hosted.httpx,
            "Client",
            lambda **kw: real(transport=httpx.MockTransport(counting), **kw),
        )


def _client(engine: str = "remote_http", base_url: str = "http://tts.test") -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-test-token",
        avatar_internal_token="internal-test-token",
        tts=TTSConfig(engine=engine, base_url=base_url),
    )
    return TestClient(create_app(config=cfg, deps=_Deps(config=cfg)))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "a", "name": "Anh"}]})


def _unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def test_returns_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTTS(monkeypatch, _ok)
    with _client() as client:
        r = client.get(URL, headers=AUTH)
    voice = {"id": "a", "name": "Anh", "language": None, "gender": None}
    assert (r.status_code, r.json()) == (200, {"voices": [voice], "available": True})


def test_viewer_token_alone_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTTS(monkeypatch, _ok)
    with _client() as client:
        r = client.get(URL, headers=VIEWER)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "voices_not_authorized")


def test_wrong_internal_token_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTTS(monkeypatch, _ok)
    with _client() as client:
        r = client.get(URL, headers={**VIEWER, "X-Livento-Internal-Token": "nope"})
    assert r.status_code == 403


def test_missing_viewer_token_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTTS(monkeypatch, _ok)
    with _client() as client:
        r = client.get(URL, headers={"X-Livento-Internal-Token": "internal-test-token"})
    assert r.status_code == 401


def test_forbidden_request_never_reaches_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTTS(monkeypatch, _ok)
    with _client() as client:
        client.get(URL, headers=VIEWER)
    assert fake.calls == 0


@pytest.mark.parametrize(
    "handler",
    [
        _unavailable,
        lambda request: httpx.Response(200, text="<html>garbage"),
        lambda request: httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_tts_failure_is_available_false_not_5xx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    FakeTTS(monkeypatch, handler)
    with _client() as client:
        r = client.get(URL, headers=AUTH)
    assert (r.status_code, r.json()) == (200, DOWN)


def test_unreachable_tts_is_available_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    FakeTTS(monkeypatch, refuse)
    with _client() as client:
        r = client.get(URL, headers=AUTH)
    assert r.json() == DOWN


def test_failure_log_has_no_url_or_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    FakeTTS(monkeypatch, _unavailable)
    with _client() as client:
        client.get(URL, headers=AUTH)
    assert "tts.test" not in caplog.text and "viewer-test-token" not in caplog.text


@pytest.mark.parametrize("engine", ["tone", "transformers", "elevenlabs"])
def test_non_remote_engine_is_unavailable_without_upstream_call(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    fake = FakeTTS(monkeypatch, _ok)
    with _client(engine=engine) as client:
        r = client.get(URL, headers=AUTH)
    assert (r.json(), fake.calls) == (DOWN, 0)


def test_missing_base_url_is_unavailable() -> None:
    with _client(base_url="") as client:
        r = client.get(URL, headers=AUTH)
    assert r.json() == DOWN


def test_success_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTTS(monkeypatch, _ok)
    with _client() as client:
        client.get(URL, headers=AUTH)
        client.get(URL, headers=AUTH)
    assert fake.calls == 1


def test_success_cache_expires_after_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTTS(monkeypatch, _ok)
    now = [1000.0]
    monkeypatch.setattr(tts_voices.time, "monotonic", lambda: now[0])
    with _client() as client:
        client.get(URL, headers=AUTH)
        now[0] += 59
        client.get(URL, headers=AUTH)
        now[0] += 2
        client.get(URL, headers=AUTH)
    assert fake.calls == 2


def test_failure_is_cached_for_10s_then_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTTS(monkeypatch, _unavailable)
    now = [1000.0]
    monkeypatch.setattr(tts_voices.time, "monotonic", lambda: now[0])
    with _client() as client:
        client.get(URL, headers=AUTH)
        now[0] += 9
        again = client.get(URL, headers=AUTH)
        calls_within_ttl = fake.calls
        now[0] += 2
        client.get(URL, headers=AUTH)
    assert (again.json(), calls_within_ttl, fake.calls) == (DOWN, 1, 2)
