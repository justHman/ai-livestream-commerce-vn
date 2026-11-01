"""BYOK at the HTTP edge: tenant start, envelope rejections, stable error codes, credential check."""

from __future__ import annotations

import base64
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from backend.application.clients.avatar import byok
from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.render.engines_base import StartResult
from backend.config import AppConfig
from conftest import make_deps as _Deps

KEY = bytes(range(32))
TRANSIT = base64.b64encode(KEY).decode()
API_KEY = "sk_tenant_secret_42"
TENANT = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
AGENT = "agent_093d2f9535db94ef"
HEADERS = {"Authorization": "Bearer viewer-t", "X-Livento-Internal-Token": "internal-t"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "RENDER_BACKEND": "mock",
        "LLM_ENGINE": "none",
        "TTS_ENGINE": "tone",
        "SESSION_STORE": "memory",
        "DIRECTOR_ENABLED": "0",
        "APP_ENV": "dev",
    }.items():
        monkeypatch.setenv(k, v)


def seal(session_id=SESSION, tenant_id=TENANT, provider="lemonslice", exp=None, **over) -> dict:
    exp = int(time.time()) + 100 if exp is None else exp
    nonce = os.urandom(12)  # unique per envelope: the Runtime refuses a replayed nonce
    plain = json.dumps({"api_key": API_KEY, "provider": provider}, separators=(",", ":"))
    aad = f"{session_id}|{tenant_id}|{provider}|{exp}".encode()
    ct = AESGCM(KEY).encrypt(nonce, plain.encode(), aad)
    env = {
        "v": 1,
        "alg": "A256GCM",
        "kid": "t1",
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode(),
        "exp": exp,
    }
    return env | over


class Capture:
    name = "capture"
    avatar_override_mode = "avatar_and_voice"
    fail: Exception | None = None

    def start(self, opts):
        self.opts = opts
        if self.fail is not None:
            raise self.fail
        return StartResult("capture-1", "ws://livekit", "token", "CAPTURE")

    def stop(self, session_id):
        return None


def client(backend=None, **cfg_over) -> TestClient:
    from backend.main import create_app

    base = dict(
        render_backend="mock",
        app_env="dev",
        backend_api_token="viewer-t",
        avatar_internal_token="internal-t",
        avatar_byok_enabled=True,
        avatar_cred_transit_key=TRANSIT,
    )
    cfg = AppConfig(**(base | cfg_over))
    d = _Deps(config=cfg)
    d.config = cfg
    if backend is not None:
        d.backend = backend
    return TestClient(create_app(config=cfg, deps=d))


def start_body(**over) -> dict:
    body = {
        "avatar_provider": "lemonslice",
        "avatar_agent_id": AGENT,
        "voice_id": "Hai Dang",
        "avatar_credential_source": "tenant",
        "avatar_credential_session_id": SESSION,
        "avatar_credential_tenant_id": TENANT,
        "avatar_credential_envelope": seal(),
    }
    return body | over


def test_tenant_start_hands_the_decrypted_key_to_the_backend_only():
    backend = Capture()
    with client(backend) as c:
        r = c.post("/api/v1/sessions", json=start_body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    assert backend.opts.extra["avatar_credential"].api_key == API_KEY


def test_tenant_start_response_never_contains_the_key_or_the_envelope():
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=start_body(), headers=HEADERS)
    assert API_KEY not in r.text and "nonce" not in r.text


def test_tenant_start_reports_the_override_as_applied():
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=start_body(), headers=HEADERS)
    assert r.json()["avatar_override_applied"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("avatar_credential_envelope"),  # tenant without envelope
        lambda b: b.update(avatar_credential_session_id="other"),  # AAD mismatch
        lambda b: b.update(avatar_credential_tenant_id="other"),
        lambda b: b.update(avatar_credential_envelope=seal(exp=int(time.time()) - 5)),
        lambda b: b.update(avatar_credential_envelope=seal(exp=int(time.time()) + 900)),
        lambda b: b.update(avatar_credential_envelope=seal(kid="zz")),
        lambda b: b.update(avatar_credential_envelope=seal(provider="simli")),
    ],
)
def test_bad_tenant_start_is_a_400_with_one_public_code(mutate):
    body = start_body()
    mutate(body)
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_platform_start_with_an_envelope_is_a_400():
    body = start_body(avatar_credential_source="platform")
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_tenant_start_with_the_flag_off_is_a_400():
    with client(Capture(), avatar_byok_enabled=False) as c:
        r = c.post("/api/v1/sessions", json=start_body(), headers=HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_tenant_start_without_the_internal_token_is_forbidden():
    with client(Capture()) as c:
        r = c.post(
            "/api/v1/sessions",
            json=start_body(),
            headers={"Authorization": "Bearer viewer-t"},
        )
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("provider", "agent"),
    [
        ("simli", "11111111-2222-4333-8444-555555555555"),
        ("bithuman", "agent_code_1"),
    ],
)
def test_other_providers_are_accepted_with_their_own_id_format(provider, agent):
    body = start_body(
        avatar_provider=provider,
        avatar_agent_id=agent,
        avatar_credential_envelope=seal(provider=provider),
    )
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text


def test_an_invalid_avatar_id_for_the_provider_is_rejected():
    body = start_body(avatar_provider="simli", avatar_credential_envelope=seal(provider="simli"))
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "code",
    [
        "AVATAR_KEY_INVALID",
        "AVATAR_OUT_OF_CREDIT",
        "AVATAR_NOT_FOUND",
        "AVATAR_PROVIDER_UNAVAILABLE",
        "AVATAR_CONCURRENCY_LIMIT",
    ],
)
def test_backend_failure_surfaces_only_the_stable_code(code):
    backend = Capture()
    backend.fail = LemonSliceError(code, f"{code}: provider text {API_KEY}")
    with client(backend) as c:
        r = c.post("/api/v1/sessions", json=start_body(), headers=HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (502, code)


# ---- credential check ------------------------------------------------------------------

CHECK = "/internal/v1/avatar/credential-check"
CHECK_ID = "44444444-4444-4444-8444-444444444444"


def check_body(**over) -> dict:
    body = {
        "provider": "simli",
        "tenant_id": TENANT,
        "check_id": CHECK_ID,
        "avatar_credential_envelope": seal(session_id=f"check:{CHECK_ID}", provider="simli"),
    }
    return body | over


@pytest.fixture
def fake_check(monkeypatch):
    seen: dict = {}

    async def fake(provider, api_key, avatar_id, **kw):
        seen.update(provider=provider, api_key=api_key, avatar_id=avatar_id)
        return "ok"

    monkeypatch.setattr(byok, "check_credential", fake)
    return seen


def test_check_returns_the_provider_verdict(fake_check):
    with client() as c:
        r = c.post(CHECK, json=check_body(), headers={"X-Livento-Internal-Token": "internal-t"})
    assert (r.status_code, r.json()) == (200, {"code": "ok"})


def test_check_passes_the_decrypted_key_to_the_provider_call_only(fake_check):
    with client() as c:
        r = c.post(
            CHECK,
            json=check_body(avatar_id="11111111-2222-4333-8444-555555555555"),
            headers={"X-Livento-Internal-Token": "internal-t"},
        )
    assert fake_check["api_key"] == API_KEY and API_KEY not in r.text


def test_check_requires_the_internal_token(fake_check):
    with client() as c:
        r = c.post(CHECK, json=check_body(), headers={"X-Livento-Internal-Token": "nope"})
    assert r.status_code == 403


def test_a_start_envelope_cannot_be_replayed_as_a_check(fake_check):
    replay = seal(session_id=SESSION, provider="simli")
    with client() as c:
        r = c.post(
            CHECK,
            json=check_body(avatar_credential_envelope=replay),
            headers={"X-Livento-Internal-Token": "internal-t"},
        )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_check_with_the_flag_off_is_a_400(fake_check):
    with client(avatar_byok_enabled=False) as c:
        r = c.post(CHECK, json=check_body(), headers={"X-Livento-Internal-Token": "internal-t"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_a_replayed_start_envelope_is_refused():
    body = start_body()
    with client(Capture()) as c:
        first = c.post("/api/v1/sessions", json=body, headers=HEADERS)
        second = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert (first.status_code, second.status_code, second.json()["error"]["code"]) == (
        200,
        400,
        "AVATAR_ENVELOPE_INVALID",
    )


def test_a_replayed_check_envelope_is_refused(fake_check):
    body = check_body()
    h = {"X-Livento-Internal-Token": "internal-t"}
    with client() as c:
        first = c.post(CHECK, json=body, headers=h)
        second = c.post(CHECK, json=body, headers=h)
    assert (first.status_code, second.status_code) == (200, 400)


def test_a_wrong_check_token_uses_its_own_bucket_and_never_drains_the_good_one(fake_check):
    with client() as c:
        keys: list[str] = []
        real = c.app.state.api_limiter.allow

        async def spy(key):
            keys.append(key.split(":")[0])
            return await real(key)

        c.app.state.api_limiter.allow = spy
        c.post(CHECK, json=check_body(), headers={"X-Livento-Internal-Token": "nope"})
        c.post(CHECK, json=check_body(), headers={"X-Livento-Internal-Token": "internal-t"})
    assert keys == ["avatar-check-bad", "avatar-check"]


@pytest.mark.parametrize("field", ["avatar_credential_session_id", "avatar_credential_tenant_id"])
def test_an_aad_separator_in_an_id_is_refused(field):
    body = start_body(**{field: "a|b"})
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "AVATAR_ENVELOPE_INVALID")


def test_a_422_on_start_never_reflects_the_envelope():
    env = seal()
    body = start_body(avatar_credential_envelope=env, avatar_agent_id="not-valid")
    with client(Capture()) as c:
        r = c.post("/api/v1/sessions", json=body, headers=HEADERS)
    assert r.status_code == 422 and env["ct"] not in r.text and env["nonce"] not in r.text


def test_a_422_on_check_never_reflects_the_envelope(fake_check):
    env = seal(session_id=f"check:{CHECK_ID}", provider="simli")
    body = check_body(avatar_credential_envelope=env, provider="nope")
    with client() as c:
        r = c.post(CHECK, json=body, headers={"X-Livento-Internal-Token": "internal-t"})
    assert r.status_code == 422 and env["ct"] not in r.text
