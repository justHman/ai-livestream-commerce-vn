"""BYOK wave 2 backends (Beyond Presence, Tavus, Anam, Runway): protocol doubles only.

A fake LiveKit room and a fake provider HTTP stand in for the services; keys are fake strings.
Nothing here can reach a real provider.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import jwt
import pytest

from backend.application.clients.avatar.byok import (
    AvatarCredential,
    check_credential,
    valid_avatar_params,
)
from backend.application.clients.avatar.byok_providers import (
    BACKENDS,
    ByokProviderSettings,
)
from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.clients.avatar.router import AvatarProviderRouter
from backend.application.render.engines_base import StartOptions
from backend.application.usage_evidence.cost import cost_meter
from backend.api.v1.router import StartReq
from backend.config import AppConfig

from .lemonslice_double import AVATAR, FakeRoom

pytestmark = pytest.mark.timeout(30)

TENANT_KEY = "tenant-key-999"
LK_SECRET = "lk-secret-value-456-padded-to-32-bytes"
UUID = "11111111-2222-4333-8444-555555555555"
TRANSIT = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

IDS = {
    "beyondpresence": UUID,
    "tavus": "r4067604db72",
    "anam": "avatar-abc-123",
    "runway": "rw_avatar_1",
}
SID_FIELD = {
    "beyondpresence": "id",
    "tavus": "conversation_id",
    "anam": "sessionId",
    "runway": "id",
}
START_SUFFIX = {
    "beyondpresence": "/v1/sessions",
    "tavus": "/v2/conversations",
    "anam": "/v1/engine/session",
    "runway": "/v1/realtime_sessions",
}
START_HEADERS = {
    "beyondpresence": {"x-api-key": TENANT_KEY},
    "tavus": {"x-api-key": TENANT_KEY, "Content-Type": "application/json"},
    "anam": {"Authorization": f"Bearer {TENANT_KEY}", "Content-Type": "application/json"},
    "runway": {"Authorization": f"Bearer {TENANT_KEY}", "X-Runway-Version": "2024-11-06"},
}
RATE = {"beyondpresence": 16000, "tavus": 24000, "anam": 24000, "runway": 16000}
ALL = list(IDS)


@pytest.fixture(autouse=True)
def _cost_defaults():
    yield
    cost_meter.avatar_provider = "lemonslice"
    cost_meter.configure(enabled=False)


class FakeProvider:
    """Answers the start POST (the avatar then enters the room), end POSTs and DELETEs."""

    def __init__(self, provider: str, room: FakeRoom) -> None:
        self.provider, self.room = provider, room
        self.posts: list[tuple[str, dict, dict]] = []
        self.deletes: list[tuple[str, dict]] = []
        self.start_status, self.start_body = 200, {SID_FIELD[provider]: "prov-1"}
        self.end_status = 200
        self.raise_on_start: BaseException | None = None

    def start_calls(self):
        return [c for c in self.posts if c[0].endswith(START_SUFFIX[self.provider])]

    def end_calls(self):
        return [c for c in self.posts if not c[0].endswith(START_SUFFIX[self.provider])]

    async def post(self, url, headers, body, timeout):
        self.posts.append((url, dict(headers), dict(body)))
        if url.endswith(START_SUFFIX[self.provider]):
            if self.raise_on_start:
                raise self.raise_on_start
            if self.start_status < 300:
                self.room.avatar_joins()
            return self.start_status, dict(self.start_body)
        self.room.remote_participants.pop(AVATAR, None)
        return self.end_status, {}

    async def delete(self, url, headers, timeout):
        self.deletes.append((url, dict(headers)))
        self.room.remote_participants.pop(AVATAR, None)
        return self.end_status


def make(provider: str, **kw):
    room = FakeRoom("normal", 0.05)
    rest = FakeProvider(provider, room)
    extra = {"async_http_delete": rest.delete} if provider == "runway" else {}
    backend = BACKENDS[provider](
        ByokProviderSettings(
            livekit_url="wss://lk.example.test",
            livekit_api_key="lk-key",
            livekit_api_secret=LK_SECRET,
            avatar_identity=AVATAR,
            ready_timeout_s=0.5,
            request_timeout_s=1.0,
            **kw,
        ),
        room_factory=lambda: room,
        async_http_post=rest.post,
        **extra,
    )
    return backend, room, rest


def opts(provider, avatar=None, params=None, key=TENANT_KEY) -> StartOptions:
    return StartOptions(
        avatar_provider=provider,
        avatar_agent_id=avatar or IDS[provider],
        extra={
            "avatar_credential": AvatarCredential(api_key=key, provider=provider),
            "avatar_params": params or {},
        },
    )


# ---- start request ------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ALL)
def test_start_uses_the_documented_url_and_auth_headers(provider):
    backend, _, rest = make(provider)
    backend.start(opts(provider))
    backend.stop_all()
    (url, headers, _) = rest.start_calls()[0]
    assert url.endswith(START_SUFFIX[provider]) and headers == START_HEADERS[provider]


def _body(rest):
    return rest.start_calls()[0][2]


def test_bey_body_matches_the_plugin():
    backend, _, rest = make("beyondpresence")
    backend.start(opts("beyondpresence"))
    backend.stop_all()
    body = _body(rest)
    assert {k: v for k, v in body.items() if k != "token"} == {
        "transport": "livekit",
        "avatar_id": UUID,
        "url": "wss://lk.example.test",
    }


def test_tavus_body_sends_the_default_pal_and_a_duration_cap():
    backend, _, rest = make("tavus", max_session_s=600)
    backend.start(opts("tavus"))
    backend.stop_all()
    body = _body(rest)
    props = body["properties"]
    assert (body["pal_id"], body["face_id"], props["max_call_duration"]) == (
        "pb87e71797da",
        "r4067604db72",
        660,
    )
    assert (props["livekit_ws_url"], bool(props["livekit_room_token"])) == (
        "wss://lk.example.test",
        True,
    )


def test_tavus_pal_id_param_overrides_the_default():
    backend, _, rest = make("tavus")
    backend.start(opts("tavus", params={"pal_id": "pcustom123"}))
    backend.stop_all()
    assert _body(rest)["pal_id"] == "pcustom123"


def test_anam_body_matches_the_plugin_and_takes_avatar_model():
    backend, _, rest = make("anam")
    backend.start(opts("anam", params={"avatar_model": "cara-4"}))
    backend.stop_all()
    body = _body(rest)
    assert body["personaConfig"] == {
        "type": "ephemeral",
        "name": "livento",
        "avatarId": "avatar-abc-123",
        "llmId": "CUSTOMER_CLIENT_V1",
        "avatarModel": "cara-4",
    }
    assert body["environment"]["livekitUrl"] == "wss://lk.example.test"


def test_anam_without_avatar_model_omits_it():
    backend, _, rest = make("anam")
    backend.start(opts("anam"))
    backend.stop_all()
    assert "avatarModel" not in _body(rest)["personaConfig"]


@pytest.mark.parametrize(
    ("params", "avatar"),
    [
        ({}, {"type": "custom", "avatarId": "rw_avatar_1"}),
        ({"kind": "avatar"}, {"type": "custom", "avatarId": "rw_avatar_1"}),
        ({"kind": "preset"}, {"type": "runway-preset", "presetId": "rw_avatar_1"}),
    ],
)
def test_runway_body_picks_custom_or_preset(params, avatar):
    backend, _, rest = make("runway", max_session_s=300)
    backend.start(opts("runway", params=params))
    backend.stop_all()
    body = _body(rest)
    assert (body["model"], body["avatar"], body["maxDuration"]) == ("gwm1_avatars", avatar, 360)
    lk = body["livekit"]
    assert lk["agentIdentity"] == "livento-runtime" and lk["roomName"].startswith("rt_")


@pytest.mark.parametrize("provider", ALL)
def test_avatar_jwt_joins_as_the_publish_on_behalf_agent(provider):
    backend, _, rest = make(provider)
    backend.start(opts(provider))
    backend.stop_all()
    body = _body(rest)
    token = body.get("token") or body.get("livekitToken")
    token = token or body.get("livekit", {}).get("token")
    token = token or body.get("properties", {}).get("livekit_room_token")
    token = token or body.get("environment", {}).get("livekitToken")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert (claims["kind"], claims["sub"], claims["attributes"]) == (
        "agent",
        AVATAR,
        {"lk.publish_on_behalf": "livento-runtime"},
    )


@pytest.mark.parametrize("provider", ALL)
def test_sample_rate_is_per_backend_with_an_override(provider):
    default, _, _ = make(provider)
    custom, _, _ = make(provider, audio_sample_rate=48000)
    rates = (default._s.audio_sample_rate, custom._s.audio_sample_rate)
    default.stop_all()
    custom.stop_all()
    assert rates == (RATE[provider], 48000)


# ---- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ALL)
def test_a_bad_avatar_id_is_refused_without_a_call(provider):
    backend, _, rest = make(provider)
    with pytest.raises(LemonSliceError) as err:
        backend.start(opts(provider, avatar="a b"))
    backend.stop_all()
    assert (err.value.code, rest.posts) == ("agent_id_invalid", [])


@pytest.mark.parametrize(
    ("provider", "params"),
    [
        ("beyondpresence", {"pal_id": "x1"}),
        ("tavus", {"avatar_model": "x1"}),
        ("tavus", {"pal_id": "x1", "extra": "y"}),
        ("anam", {"kind": "avatar"}),
        ("runway", {"kind": "other"}),
        ("runway", {"kind": "a b"}),
    ],
)
def test_unknown_or_bad_params_are_refused_without_a_call(provider, params):
    backend, _, rest = make(provider)
    with pytest.raises(LemonSliceError) as err:
        backend.start(opts(provider, params=params))
    backend.stop_all()
    assert (err.value.code, rest.posts) == ("avatar_params_invalid", [])


def test_param_rules():
    assert valid_avatar_params("tavus", {"pal_id": "pb87e71797da"}) is True
    assert valid_avatar_params("tavus", {"pal_id": "p" * 65}) is False
    assert valid_avatar_params("lemonslice", {"pal_id": "x"}) is False
    assert valid_avatar_params("tavus", None) is True


@pytest.mark.parametrize("provider", ALL)
def test_a_platform_session_is_refused(provider):
    backend, _, rest = make(provider)
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions(avatar_provider=provider, avatar_agent_id=IDS[provider]))
    backend.stop_all()
    assert (err.value.code, rest.posts) == ("avatar_credential_required", [])


@pytest.mark.parametrize("provider", ALL)
def test_another_provider_name_is_refused(provider):
    backend, _, rest = make(provider)
    bad = opts(provider)
    bad = StartOptions(avatar_provider="simli", avatar_agent_id=IDS[provider], extra=bad.extra)
    with pytest.raises(LemonSliceError) as err:
        backend.start(bad)
    backend.stop_all()
    assert (err.value.code, rest.posts) == ("avatar_provider_invalid", [])


def _req(**kw):
    base = dict(
        avatar_provider="tavus",
        avatar_agent_id="r4067604db72",
        voice_id="v1",
        avatar_params={"pal_id": "pb87e71797da"},
    )
    return StartReq(**(base | kw))


def test_start_req_accepts_the_wave2_providers_and_params():
    assert _req().avatar_params == {"pal_id": "pb87e71797da"}


@pytest.mark.parametrize(
    "kw",
    [
        {"avatar_params": {"unknown": "x1"}},
        {"avatar_params": {"pal_id": "bad value"}},
        {
            "avatar_provider": "anam",
            "avatar_agent_id": "avatar-1",
            "avatar_params": {"pal_id": "x"},
        },
        {"avatar_agent_id": "a b"},
    ],
)
def test_start_req_rejects_bad_params_and_ids(kw):
    with pytest.raises(ValueError):
        _req(**kw)


# ---- end of session -----------------------------------------------------------------------


def test_tavus_stop_ends_the_conversation_with_the_tenant_key():
    backend, _, rest = make("tavus")
    res = backend.start(opts("tavus"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert rest.end_calls() == [
        ("https://tavusapi.com/v2/conversations/prov-1/end", {"x-api-key": TENANT_KEY}, {})
    ]


def test_anam_stop_posts_the_stop_endpoint_with_a_bearer_key():
    backend, _, rest = make("anam")
    res = backend.start(opts("anam"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert rest.end_calls() == [
        (
            "https://api.anam.ai/v1/sessions/prov-1/stop",
            {"Authorization": f"Bearer {TENANT_KEY}"},
            {},
        )
    ]


def test_runway_stop_deletes_the_session_and_sends_end_call():
    backend, room, rest = make("runway")
    sent: list = []

    async def publish_data(payload, **kw):
        sent.append((payload, kw))

    room.local_participant.publish_data = publish_data
    res = backend.start(opts("runway"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert rest.deletes == [
        (
            "https://api.dev.runwayml.com/v1/realtime_sessions/prov-1",
            START_HEADERS["runway"],
        )
    ]
    assert sent == [
        (b'{"type": "END_CALL"}', {"reliable": True, "destination_identities": [AVATAR]})
    ]


def test_runway_stop_still_deletes_when_end_call_fails():
    backend, room, rest = make("runway")

    async def boom(payload, **kw):
        raise OSError("no data channel")

    room.local_participant.publish_data = boom
    res = backend.start(opts("runway"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert len(rest.deletes) == 1


def test_bey_stop_leaves_the_room_and_calls_no_end_api():
    backend, room, rest = make("beyondpresence")
    res = backend.start(opts("beyondpresence"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert (rest.end_calls(), room.disconnected) == ([], True)


@pytest.mark.parametrize("provider", ALL)
def test_the_tenant_key_is_dropped_once_the_session_ended(provider):
    backend, _, _ = make(provider)
    res = backend.start(opts(provider))
    held = dict(backend._tenant_keys)
    backend.stop(res.session_id)
    after = dict(backend._tenant_keys)
    backend.stop_all()
    assert (held, after) == ({"prov-1": TENANT_KEY}, {})


def test_a_failed_end_keeps_the_key_for_the_retry():
    backend, _, rest = make("tavus")
    res = backend.start(opts("tavus"))
    rest.end_status = 500
    with pytest.raises(Exception):  # noqa: B017 - ProviderCleanupIncomplete
        backend.stop(res.session_id)
    held = dict(backend._tenant_keys)
    rest.end_status = 200
    backend.stop(res.session_id)
    backend.stop_all()
    assert held == {"prov-1": TENANT_KEY}


# ---- error mapping ------------------------------------------------------------------------

COMMON = [
    (401, {}, "AVATAR_KEY_INVALID"),
    (403, {}, "AVATAR_KEY_INVALID"),
    (402, {}, "AVATAR_OUT_OF_CREDIT"),
    (404, {}, "AVATAR_NOT_FOUND"),
    (429, {}, "AVATAR_CONCURRENCY_LIMIT"),
    (503, {}, "AVATAR_PROVIDER_UNAVAILABLE"),
]
SPECIFIC = {
    "beyondpresence": [(422, {"detail": [{"msg": "bad"}]}, "AVATAR_NOT_FOUND")],
    "tavus": [
        (400, {"message": "Max concurrent conversations reached"}, "AVATAR_CONCURRENCY_LIMIT"),
        (400, {"message": "Out of minutes"}, "AVATAR_OUT_OF_CREDIT"),
        (400, {"message": "face not found"}, "AVATAR_NOT_FOUND"),
    ],
    "anam": [(400, {}, "AVATAR_NOT_FOUND"), (422, {}, "AVATAR_NOT_FOUND")],
    "runway": [
        (400, {"error": "Not enough credits"}, "AVATAR_OUT_OF_CREDIT"),
        (422, {"error": "bad avatar"}, "AVATAR_NOT_FOUND"),
    ],
}
CASES = [(p, *c) for p in ALL for c in COMMON + SPECIFIC[p]]


@pytest.mark.parametrize(("provider", "status", "body", "code"), CASES)
def test_statuses_map_to_stable_codes(provider, status, body, code):
    backend, _, rest = make(provider)
    rest.start_status, rest.start_body = status, body
    with pytest.raises(LemonSliceError) as err:
        backend.start(opts(provider))
    backend.stop_all()
    assert err.value.code == code and backend._credit_until == 0.0


@pytest.mark.parametrize("provider", ALL)
def test_network_failure_is_provider_unavailable_and_leaks_nothing(provider, caplog):
    backend, _, rest = make(provider)
    rest.raise_on_start = OSError("https://x.test/?k=" + TENANT_KEY)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LemonSliceError) as err:
            backend.start(opts(provider))
    backend.stop_all()
    assert err.value.code == "AVATAR_PROVIDER_UNAVAILABLE"
    assert TENANT_KEY not in str(err.value) and TENANT_KEY not in caplog.text


@pytest.mark.parametrize("provider", ALL)
def test_a_missing_session_id_fails_the_start(provider):
    backend, _, rest = make(provider)
    rest.start_body = {}
    with pytest.raises(LemonSliceError) as err:
        backend.start(opts(provider))
    backend.stop_all()
    assert err.value.code == "session_id_invalid"


# ---- cost, platform guards ----------------------------------------------------------------


@pytest.mark.parametrize("provider", ALL)
def test_cost_row_carries_the_provider_label_and_tenant_source(provider, monkeypatch):
    seen: list = []
    cost_meter.configure(enabled=True)
    monkeypatch.setattr(cost_meter, "_push", lambda *a, **k: seen.append(k))
    backend, _, _ = make(provider)
    res = backend.start(opts(provider))
    backend.stop(res.session_id)
    backend.stop_all()
    assert [(k["provider"], k["credential_source"]) for k in seen] == [(provider, "tenant")]


@pytest.mark.parametrize("provider", ALL)
def test_a_tenant_402_never_pauses_platform_creation(provider):
    backend, _, rest = make(provider)
    rest.start_status = 402
    with pytest.raises(LemonSliceError):
        backend.start(opts(provider))
    backend.stop_all()
    assert backend._credit_until == 0.0


# Beyond Presence has no end call (it only leaves the room), so no key is ever sent after start
@pytest.mark.parametrize("provider", ["tavus", "anam", "runway"])
def test_a_lost_tenant_key_fails_closed_and_never_falls_back(provider):
    backend, _, rest = make(provider)
    res = backend.start(opts(provider))
    backend._tenant_keys.clear()  # the key vanished; the session is still a tenant session
    with pytest.raises(Exception):  # noqa: B017 - ProviderCleanupIncomplete
        backend.stop(res.session_id)
    backend.stop_all()
    assert rest.end_calls() == [] and rest.deletes == []


@pytest.mark.parametrize("provider", ALL)
def test_a_wave2_envelope_opens_once_through_the_shared_replay_cache(provider):
    import base64
    import json
    import os
    import time

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from backend.api.v1.avatar_credentials import open_tenant_credential
    from fastapi import HTTPException

    key = bytes(range(32))
    exp, nonce = int(time.time()) + 100, os.urandom(12)
    aad = f"sess-1|ten-1|{provider}|{exp}".encode()
    plain = json.dumps({"api_key": TENANT_KEY, "provider": provider}).encode()
    envelope = {
        "v": 1,
        "alg": "A256GCM",
        "kid": "t1",
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(AESGCM(key).encrypt(nonce, plain, aad)).decode(),
        "exp": exp,
    }
    config = SimpleNamespace(
        avatar_byok_enabled=True,
        avatar_cred_transit_key=base64.b64encode(key).decode(),
        avatar_cred_transit_key_id="t1",
    )

    def open_():
        return open_tenant_credential(
            config,
            session_ref="sess-1",
            tenant_id="ten-1",
            provider=provider,
            envelope=envelope,
        )

    first = open_()
    with pytest.raises(HTTPException) as err:
        open_()
    assert (first.provider, err.value.status_code) == (provider, 400)


# ---- router / flags -----------------------------------------------------------------------


def cfg(**kw) -> AppConfig:
    base = dict(
        app_env="dev",
        render_backend="mock",
        livekit_url="wss://lk.example.test",
        livekit_api_key="lk-key",
        livekit_api_secret=LK_SECRET,
        avatar_byok_enabled=True,
        avatar_cred_transit_key=TRANSIT,
        lemonslice_api_key="platform-key",
        lemonslice_agent_id="agent_093d2f9535db94ef",
    )
    return AppConfig(**(base | kw))


FLAGS = {
    "beyondpresence": "beyond_presence_enabled",
    "tavus": "tavus_enabled",
    "anam": "anam_enabled",
    "runway": "runway_enabled",
}


@pytest.mark.parametrize("provider", ALL)
def test_flag_off_is_not_a_tenant_provider(provider):
    router = cfg()._build_avatar_router(SimpleNamespace(name="cloud_lemonslice"))
    with pytest.raises(LemonSliceError) as err:
        router.start(opts(provider))
    assert err.value.code == "AVATAR_PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize("provider", ALL)
def test_flag_on_builds_the_backend_for_tenants(provider):
    router = cfg(**{FLAGS[provider]: True})._build_avatar_router(
        SimpleNamespace(name="cloud_lemonslice")
    )
    assert type(router._by_provider[provider]) is BACKENDS[provider]
    assert [p for p in router._by_provider if p in ALL] == [provider]


@pytest.mark.parametrize("provider", ALL)
def test_router_sends_a_tenant_session_to_the_wave2_backend_and_routes_stop(provider):
    backend, _, rest = make(provider)
    default = SimpleNamespace(name="cloud_lemonslice", stop=lambda s: None, stop_all=lambda: None)
    router = AvatarProviderRouter(default, {provider: backend})
    res = router.start(opts(provider))
    ids = router.session_ids()
    router.stop(res.session_id)
    router.stop_all()
    assert (len(rest.start_calls()), res.session_id in ids, backend.session_ids()) == (1, True, ())


# ---- credential check ---------------------------------------------------------------------


class FakeGet:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.calls = status, data if data is not None else {}, []

    async def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        return self.status, self.data


BASES = {
    "beyondpresence": "https://api.bey.dev",
    "tavus": "https://tavusapi.com",
    "anam": "https://api.anam.ai",
    "runway": "https://api.dev.runwayml.com",
}


async def _check(provider, get, avatar_id=None):
    return await check_credential(
        provider,
        TENANT_KEY,
        avatar_id,
        simli_base="",
        wave2_bases=BASES,
        http_get=get,
    )


@pytest.mark.parametrize("provider", ALL)
async def test_check_sends_a_read_only_get_with_the_key(provider):
    get = FakeGet()
    assert await _check(provider, get) == "ok"
    (url, headers) = get.calls[0]
    assert url.startswith(BASES[provider]) and TENANT_KEY in "".join(headers.values())


@pytest.mark.parametrize("provider", ALL)
@pytest.mark.parametrize(
    ("status", "code"), [(401, "invalid_key"), (403, "invalid_key"), (500, "provider_error")]
)
async def test_check_maps_provider_statuses(provider, status, code):
    assert await _check(provider, FakeGet(status)) == code


@pytest.mark.parametrize("provider", ALL)
async def test_check_with_an_unlistable_avatar_is_unknown(provider):
    assert await _check(provider, FakeGet(200, {"data": []}), IDS[provider]) == "unknown"


async def test_check_finds_a_listed_avatar():
    assert (
        await _check("anam", FakeGet(200, {"data": [{"id": "avatar-abc-123"}]}), "avatar-abc-123")
        == "ok"
    )


async def test_check_with_a_malformed_avatar_id_is_not_found_without_a_call():
    get = FakeGet()
    assert await _check("tavus", get, "a b") == "not_found" and get.calls == []


async def test_check_with_an_unconfigured_base_is_unknown():
    assert (
        await check_credential("tavus", TENANT_KEY, None, simli_base="", http_get=FakeGet())
        == "unknown"
    )
