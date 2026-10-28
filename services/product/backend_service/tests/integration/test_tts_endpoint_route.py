"""/api/v1/tts/endpoint (operator-set TTS server address) and its effect on the TTS client. Redis and the TTS server are fakes."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.v1 import tts_endpoint
from backend.application.clients.tts import endpoint_override, self_hosted
from backend.config import AppConfig, TTSConfig
from conftest import make_deps as _Deps

URL = "/api/v1/tts/endpoint"
TOKEN = "t" * 40
ADMIN = {"X-Livento-Tts-Admin": TOKEN, "X-Livento-Actor": "admin@livento.id.vn"}
ENV_URL = "http://env.tts.test:8000"
NEW_URL = "http://203.0.113.9:52226"


async def _never_answers(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)
    return httpx.Response(200)


class FakeRedis:
    store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    FakeRedis.store = {}
    monkeypatch.setenv("TTS_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv(
        "TTS_AUTH_TOKEN", "gpu-secret-token"
    )  # production keeps the GPU key in the env only
    monkeypatch.setattr(endpoint_override, "_redis", lambda: FakeRedis())
    monkeypatch.setattr(endpoint_override, "_cache", None)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.headers.get("authorization") != "Bearer gpu-secret-token":
            return httpx.Response(401, text="unauthorized")
        if request.url.host == "203.0.113.77":
            return _never_answers(request)
        if request.url.host == "203.0.113.66":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"data": [{"id": "a", "name": "Anh"}]})

    real = httpx.Client
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_async(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(
        self_hosted.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


def _client(engine: str = "remote_http") -> TestClient:
    from backend.main import create_app

    cfg = AppConfig(
        render_backend="mock", app_env="dev", tts=TTSConfig(engine=engine, base_url=ENV_URL)
    )
    return TestClient(create_app(config=cfg, deps=_Deps(config=cfg)))


def test_off_without_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTS_ADMIN_TOKEN")
    with _client() as client:
        assert client.get(URL, headers=ADMIN).status_code == 404


def test_wrong_token_is_forbidden() -> None:
    with _client() as client:
        r = client.get(URL, headers={**ADMIN, "X-Livento-Tts-Admin": "x" * 40})
    assert r.status_code == 403


def test_actor_is_required() -> None:
    with _client() as client:
        r = client.get(URL, headers={"X-Livento-Tts-Admin": TOKEN})
    assert r.status_code == 400


def test_get_reports_env_address_by_default() -> None:
    with _client() as client:
        body = client.get(URL, headers=ADMIN).json()
    assert (body["base_url"], body["source"], body["reachable"]) == (ENV_URL, "env", True)


def test_put_stores_probed_address_and_get_returns_it() -> None:
    with _client() as client:
        put = client.put(URL, headers=ADMIN, json={"base_url": NEW_URL + "/"})
        got = client.get(URL, headers=ADMIN).json()
    assert (put.status_code, got["base_url"], got["source"], got["updated_by"]) == (
        200,
        NEW_URL,
        "override",
        "admin@livento.id.vn",
    )


def test_put_refuses_unreachable_address_and_keeps_the_old_one() -> None:
    with _client() as client:
        r = client.put(URL, headers=ADMIN, json={"base_url": "http://203.0.113.66:1"})
        got = client.get(URL, headers=ADMIN).json()
    assert (r.status_code, r.json()["error"]["code"], got["source"]) == (
        422,
        "tts_unreachable",
        "env",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ftp://203.0.113.9",
        "http://127.0.0.1:9",
        "http://localhost:9",
        "http://169.254.169.254",
        "http://u:p@203.0.113.9",
        "http://203.0.113.9/x",
        "http://203.0.113.9?a=1",
        "http://[::1]:9",
        "http://0177.0.0.1",
    ],
)
def test_put_refuses_unsafe_or_malformed_address(bad: str) -> None:
    with _client() as client:
        r = client.put(URL, headers=ADMIN, json={"base_url": bad})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_base_url")


def test_delete_returns_to_env_address() -> None:
    with _client() as client:
        client.put(URL, headers=ADMIN, json={"base_url": NEW_URL})
        r = client.delete(URL, headers=ADMIN)
    assert (r.status_code, r.json()["source"]) == (200, "env")


def test_not_remote_engine_is_conflict() -> None:
    with _client(engine="tone") as client:
        assert client.get(URL, headers=ADMIN).status_code == 409


def test_client_follows_override(_env: list[str]) -> None:
    endpoint_override.write_override(NEW_URL, "op")
    assert self_hosted.SelfHostedTTSClient(base_url=ENV_URL)._base_url == NEW_URL


def test_client_falls_back_to_env_when_redis_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(endpoint_override, "_redis", broken)
    assert self_hosted.SelfHostedTTSClient(base_url=ENV_URL)._base_url == ENV_URL


def test_client_ignores_override_when_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint_override.write_override(NEW_URL, "op")
    monkeypatch.delenv("TTS_ADMIN_TOKEN")
    assert self_hosted.SelfHostedTTSClient(base_url=ENV_URL)._base_url == ENV_URL


def test_probe_has_a_real_deadline_and_leaves_nothing_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tts_endpoint, "_PROBE_TIMEOUT_S", 0.2)
    with _client() as client:
        slow = client.put(URL, headers=ADMIN, json={"base_url": "http://203.0.113.77:1"})
        again = client.put(URL, headers=ADMIN, json={"base_url": NEW_URL})
    assert (slow.status_code, slow.json()["error"]["code"], again.status_code) == (
        422,
        "tts_unreachable",
        200,
    )
