"""BYOK on the avatar backends: per-session tenant key, platform guards, router, bitHuman.

Protocol doubles only (fake LiveKit room, fake provider HTTP); keys are fake strings.
"""

from __future__ import annotations

import pytest
import jwt

from backend.application.clients.avatar.bithuman import (
    BitHumanRenderBackend,
    BitHumanSettings,
)
from backend.application.clients.avatar.byok import AvatarCredential
from backend.application.clients.avatar.lemonslice import (
    LemonSliceError,
    LemonSliceRenderBackend,
    LemonSliceSettings,
)
from backend.application.clients.avatar.router import AvatarProviderRouter
from backend.application.clients.avatar.simli import SimliRenderBackend, SimliSettings
from backend.application.render.engines_base import StartOptions
from backend.application.usage_evidence.cost import cost_meter
from backend.config import AppConfig

from .lemonslice_double import AVATAR, FakeLemonSlice, FakeRoom
from .simli_double import FakeSimli

pytestmark = pytest.mark.timeout(30)

PLATFORM_KEY = "platform-key-111"
TENANT_KEY = "tenant-key-999"
LK_SECRET = "lk-secret-value-456-padded-to-32-bytes"
AGENT = "agent_093d2f9535db94ef"
FACE = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _cost_defaults():
    yield
    cost_meter.avatar_provider = "lemonslice"
    cost_meter.configure(enabled=False)


def tenant_opts(provider, avatar_id, key=TENANT_KEY) -> StartOptions:
    return StartOptions(
        avatar_provider=provider,
        avatar_agent_id=avatar_id,
        extra={"avatar_credential": AvatarCredential(api_key=key, provider=provider)},
    )


# ---- LemonSlice -----------------------------------------------------------------------


def make_ls(status=200):
    room = FakeRoom("normal", 0.05)
    rest = FakeLemonSlice(room, status)
    keys: list[str] = []

    def key_client_factory(key):
        keys.append(key)
        return rest.client_factory

    backend = LemonSliceRenderBackend(
        LemonSliceSettings(
            livekit_url="wss://lk.example.test",
            livekit_api_key="lk-key",
            livekit_api_secret=LK_SECRET,
            lemonslice_api_key=PLATFORM_KEY,
            agent_id="agent-preset",
            ready_timeout_s=0.5,
            playback_margin_s=0.3,
        ),
        room_factory=lambda: room,
        session_client_factory=rest.client_factory,
        key_client_factory=key_client_factory,
        http_post=rest,
    )
    return backend, room, rest, keys


def test_tenant_session_starts_with_a_client_built_for_its_key():
    backend, _, _, keys = make_ls()
    backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop_all()
    assert keys == [TENANT_KEY]


def test_tenant_session_terminates_with_the_tenant_key():
    backend, _, rest, _ = make_ls()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop(res.session_id)
    backend.stop_all()
    terminate = [h for _, h, b in rest.calls if b == {"event": "terminate"}]
    assert terminate == [{"X-API-Key": TENANT_KEY}]


def test_platform_session_keeps_using_the_platform_key():
    backend, _, rest, keys = make_ls()
    res = backend.start(StartOptions())
    backend.stop(res.session_id)
    backend.stop_all()
    assert keys == [] and {h["X-API-Key"] for _, h, _ in rest.calls} == {PLATFORM_KEY}


def test_the_tenant_key_is_dropped_once_the_provider_confirms_the_end():
    backend, _, _, _ = make_ls()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop(res.session_id)
    held = dict(backend._tenant_keys)
    backend.stop_all()
    assert held == {}


def test_a_tenant_out_of_credit_does_not_pause_platform_creation():
    backend, _, _, _ = make_ls(status=402)
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("lemonslice", AGENT))
    assert err.value.code == "AVATAR_OUT_OF_CREDIT"
    assert backend._credit_until == 0.0
    backend.stop_all()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "AVATAR_KEY_INVALID"),
        (404, "AVATAR_NOT_FOUND"),
        (429, "AVATAR_CONCURRENCY_LIMIT"),
        (503, "AVATAR_PROVIDER_UNAVAILABLE"),
    ],
)
def test_tenant_start_failures_carry_a_stable_code(status, code):
    backend, _, _, _ = make_ls(status=status)
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop_all()
    assert err.value.code == code


def test_a_platform_credit_trip_neither_blocks_nor_ends_a_tenant_session():
    backend, _, _, _ = make_ls()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    backend._trip_credit()
    status = backend.lifecycle_status(res.session_id, 1200.0)
    second = backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop_all()
    assert status == "" and second.session_id != res.session_id


def test_a_tenant_session_is_not_charged_to_the_platform_budget(monkeypatch):
    backend, _, _, _ = make_ls()
    calls: list = []

    class Budget:
        async def begin(self, *_):
            calls.append("begin")
            return "tok"

        async def exceeded(self):
            calls.append("exceeded")
            return True

        async def close(self):
            return None

    backend._budget = Budget()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    status = backend.lifecycle_status(res.session_id, 1200.0)
    backend.stop_all()
    assert calls == [] and status == ""


def test_tenant_cost_row_records_the_credential_source(monkeypatch):
    seen: list = []
    cost_meter.configure(enabled=True)
    monkeypatch.setattr(cost_meter, "_push", lambda *a, **k: seen.append(k))
    backend, _, _, _ = make_ls()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    backend.stop(res.session_id)
    backend.stop_all()
    assert [(k["provider"], k["credential_source"]) for k in seen] == [("lemonslice", "tenant")]


def test_platform_cost_row_has_no_credential_source(monkeypatch):
    seen: list = []
    cost_meter.configure(enabled=True)
    monkeypatch.setattr(cost_meter, "_push", lambda *a, **k: seen.append(k))
    backend, _, _, _ = make_ls()
    res = backend.start(StartOptions())
    backend.stop(res.session_id)
    backend.stop_all()
    assert "credential_source" not in seen[0]


# ---- Simli ------------------------------------------------------------------------------


def make_simli(**kw):
    room = FakeRoom("normal", 0.05)
    rest = FakeSimli(room)
    backend = SimliRenderBackend(
        SimliSettings(
            livekit_url="wss://lk.example.test",
            livekit_api_key="lk-key",
            livekit_api_secret=LK_SECRET,
            simli_api_key=PLATFORM_KEY,
            face_id=FACE,
            avatar_identity=AVATAR,
            ready_timeout_s=0.5,
            request_timeout_s=1.0,
            **kw,
        ),
        room_factory=lambda: room,
        async_http_post=rest,
    )
    return backend, room, rest


def test_simli_tenant_start_sends_the_tenant_key():
    backend, _, rest = make_simli()
    backend.start(tenant_opts("simli", FACE))
    backend.stop_all()
    assert rest.stage("token")[0][1] == {"x-simli-api-key": TENANT_KEY}


def test_simli_tenant_out_of_credit_is_a_stable_code_and_not_a_platform_trip():
    backend, _, rest = make_simli()
    rest.token_status = 402
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("simli", FACE))
    assert (err.value.code, backend._credit_until) == ("AVATAR_OUT_OF_CREDIT", 0.0)
    backend.stop_all()


def test_simli_tenant_network_failure_is_provider_unavailable():
    backend, _, rest = make_simli()
    rest.raise_on["token"] = OSError("https://api.simli.ai?k=" + TENANT_KEY)
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("simli", FACE))
    backend.stop_all()
    assert err.value.code == "AVATAR_PROVIDER_UNAVAILABLE" and TENANT_KEY not in str(err.value)


def test_simli_platform_402_still_trips_the_platform_credit():
    backend, _, rest = make_simli()
    rest.token_status = 402
    with pytest.raises(LemonSliceError):
        backend.start(StartOptions())
    assert backend._credit_until > 0
    backend.stop_all()


# ---- bitHuman ---------------------------------------------------------------------------


class FakeBitHuman:
    def __init__(self, room: FakeRoom) -> None:
        self.room = room
        self.calls: list[tuple[str, dict, dict]] = []
        self.start_status, self.start_body = 200, {"session_id": "bh-1"}
        self.end_status = 200

    async def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), dict(body)))
        if url.endswith("/v1/runtime-tokens/request"):
            if self.start_status < 300:
                self.room.avatar_joins()
            return self.start_status, dict(self.start_body)
        self.room.remote_participants.pop(AVATAR, None)
        return self.end_status, {}

    def stage(self, suffix: str):
        return [c for c in self.calls if c[0].endswith(suffix)]


def make_bh(**kw):
    room = FakeRoom("normal", 0.05)
    rest = FakeBitHuman(room)
    backend = BitHumanRenderBackend(
        BitHumanSettings(
            livekit_url="wss://lk.example.test",
            livekit_api_key="lk-key",
            livekit_api_secret=LK_SECRET,
            bithuman_api_key=PLATFORM_KEY,
            avatar_identity=AVATAR,
            ready_timeout_s=0.5,
            request_timeout_s=1.0,
            **kw,
        ),
        room_factory=lambda: room,
        async_http_post=rest,
    )
    return backend, room, rest


def test_bithuman_start_request_matches_the_documented_body():
    backend, _, rest = make_bh()
    res = backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop_all()
    (url, headers, body) = rest.stage("/v1/runtime-tokens/request")[0]
    assert url == "https://api.bithuman.ai/v1/runtime-tokens/request"
    assert headers == {"api-secret": TENANT_KEY}
    assert {k: body[k] for k in body if k != "livekit_token"} == {
        "mode": "gpu",
        "model": "essence-2",
        "agent_id": "agent_code_1",
        "livekit_url": "wss://lk.example.test",
        "room_name": res.session_id,
    }


def test_bithuman_avatar_jwt_joins_as_the_publish_on_behalf_agent():
    backend, _, rest = make_bh()
    backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop_all()
    body = rest.stage("/v1/runtime-tokens/request")[0][2]
    claims = jwt.decode(body["livekit_token"], options={"verify_signature": False})
    assert (claims["kind"], claims["sub"], claims["attributes"]) == (
        "agent",
        AVATAR,
        {"lk.publish_on_behalf": "livento-runtime"},
    )


def test_bithuman_stop_ends_the_session_with_the_tenant_key_and_no_body():
    backend, _, rest = make_bh()
    res = backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert rest.stage("/v1/runtime-sessions/bh-1/end") == [
        (
            "https://api.bithuman.ai/v1/runtime-sessions/bh-1/end",
            {"api-secret": TENANT_KEY},
            {},
        )
    ]


def test_bithuman_model_is_configurable():
    backend, _, rest = make_bh(model="essence-3")
    backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop_all()
    assert rest.stage("/v1/runtime-tokens/request")[0][2]["model"] == "essence-3"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "AVATAR_KEY_INVALID"),
        (402, {"code": "INSUFFICIENT_BALANCE"}, "AVATAR_OUT_OF_CREDIT"),
        (403, {"code": "PLAN_REQUIRED"}, "AVATAR_KEY_INVALID"),
        (403, {"code": "CONCURRENCY_LIMIT_REACHED"}, "AVATAR_CONCURRENCY_LIMIT"),
        (403, {"code": "other"}, "AVATAR_KEY_INVALID"),
        (404, {}, "AVATAR_NOT_FOUND"),
        (409, {"code": "MODEL_NOT_GENERATED"}, "AVATAR_NOT_FOUND"),
        (409, {"code": "AGENT_NOT_READY"}, "AVATAR_NOT_FOUND"),
        (503, {}, "AVATAR_PROVIDER_UNAVAILABLE"),
    ],
)
def test_bithuman_statuses_map_to_stable_codes(status, body, code):
    backend, _, rest = make_bh()
    rest.start_status, rest.start_body = status, body
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop_all()
    assert err.value.code == code


def test_bithuman_session_id_may_arrive_under_data():
    backend, _, rest = make_bh()
    rest.start_body = {"data": {"session_id": "bh-2"}}
    backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop_all()
    assert rest.stage("/v1/runtime-sessions/bh-2/end")


def test_bithuman_rejects_a_bad_agent_code_without_a_call():
    backend, _, rest = make_bh()
    with pytest.raises(LemonSliceError) as err:
        backend.start(tenant_opts("bithuman", "x"))
    backend.stop_all()
    assert (err.value.code, rest.calls) == ("agent_id_invalid", [])


def test_bithuman_is_costed_as_bithuman(monkeypatch):
    seen: list = []
    cost_meter.configure(enabled=True)
    monkeypatch.setattr(cost_meter, "_push", lambda *a, **k: seen.append(k))
    backend, _, _ = make_bh()
    res = backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend.stop(res.session_id)
    backend.stop_all()
    assert [(k["provider"], k["credential_source"]) for k in seen] == [("bithuman", "tenant")]


# ---- router -----------------------------------------------------------------------------


def make_router():
    ls, _, ls_rest, _ = make_ls()
    sm, _, sm_rest = make_simli()
    router = AvatarProviderRouter.build(ls, {"lemonslice": lambda: ls, "simli": lambda: sm})
    return router, ls, ls_rest, sm, sm_rest


def test_router_sends_a_platform_session_to_the_global_backend():
    router, _, ls_rest, _, sm_rest = make_router()
    router.start(StartOptions())
    router.stop_all()
    assert (len(ls_rest.starts), sm_rest.calls) == (1, [])


def test_router_sends_a_tenant_session_to_its_provider_backend():
    router, _, ls_rest, _, sm_rest = make_router()
    router.start(tenant_opts("simli", FACE))
    router.stop_all()
    assert (ls_rest.starts, len(sm_rest.stage("token"))) == ([], 1)


def test_router_routes_stop_to_the_backend_that_started_the_session():
    router, _, ls_rest, sm, _ = make_router()
    res = router.start(tenant_opts("simli", FACE))
    ids_before = router.session_ids()
    router.stop(res.session_id)
    router.stop_all()
    assert (res.session_id in ids_before, sm.session_ids(), ls_rest.controls) == (True, (), [])


def test_router_reports_status_of_a_tenant_session_through_its_backend():
    router, *_ = make_router()
    res = router.start(tenant_opts("simli", FACE))
    status = router.session_status(res.session_id)
    router.stop_all()
    assert status == "active"


def test_router_refuses_a_tenant_provider_without_a_backend():
    router, *_ = make_router()
    with pytest.raises(LemonSliceError) as err:
        router.start(tenant_opts("bithuman", "agent_code_1"))
    router.stop_all()
    assert err.value.code == "AVATAR_PROVIDER_UNAVAILABLE"


def test_router_keeps_the_global_backend_name_and_override_mode():
    router, ls, *_ = make_router()
    router.stop_all()
    assert (router.name, router.avatar_override_mode) == (ls.name, ls.avatar_override_mode)


def test_router_hides_probes_the_global_backend_lacks():
    class Plain:
        name = "plain"

    router = AvatarProviderRouter.build(Plain(), {})
    assert hasattr(router, "wait_speak_ready") is False


def test_router_exposes_probes_when_a_backend_has_them():
    router, *_ = make_router()
    router.stop_all()
    assert hasattr(router, "wait_speak_ready") is True


# ---- config -----------------------------------------------------------------------------

TRANSIT = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


def cfg(**kw) -> AppConfig:
    base = dict(
        app_env="dev",
        render_backend="cloud_simli",
        livekit_url="wss://lk.example.test",
        livekit_api_key="lk-key",
        livekit_api_secret=LK_SECRET,
        simli_api_key=PLATFORM_KEY,
        simli_face_id=FACE,
    )
    return AppConfig(**(base | kw))


def test_flag_off_keeps_the_plain_global_backend():
    backend = cfg().build_render_backend()
    assert type(backend) is SimliRenderBackend


def test_flag_on_wraps_the_global_backend_in_a_router():
    backend = cfg(avatar_byok_enabled=True, avatar_cred_transit_key=TRANSIT).build_render_backend()
    assert isinstance(backend, AvatarProviderRouter) and backend.name == "cloud_simli"


def test_flag_on_without_a_valid_transit_key_refuses_to_start():
    with pytest.raises(ValueError):
        cfg(avatar_byok_enabled=True, avatar_cred_transit_key="").build_render_backend()


def test_bithuman_is_not_a_tenant_provider_until_enabled():
    router = cfg(avatar_byok_enabled=True, avatar_cred_transit_key=TRANSIT).build_render_backend()
    with pytest.raises(LemonSliceError) as err:
        router.start(tenant_opts("bithuman", "agent_code_1"))
    assert err.value.code == "AVATAR_PROVIDER_UNAVAILABLE"


def test_bithuman_is_a_tenant_provider_when_enabled():
    router = cfg(
        avatar_byok_enabled=True, avatar_cred_transit_key=TRANSIT, bithuman_enabled=True
    ).build_render_backend()
    assert isinstance(router._by_provider["bithuman"], BitHumanRenderBackend)


def test_a_tenant_session_that_lost_its_key_never_falls_back_to_the_platform_key():
    backend, _, rest, _ = make_ls()
    res = backend.start(tenant_opts("lemonslice", AGENT))
    backend._tenant_keys.clear()  # the key is gone (bug / eviction): fail closed
    try:
        backend.stop(res.session_id)
    except LemonSliceError:
        pass
    backend.stop_all()
    assert PLATFORM_KEY not in {h["X-API-Key"] for _, h, _ in rest.calls}


def test_bithuman_tenant_session_that_lost_its_key_never_uses_the_platform_key():
    backend, _, rest = make_bh()
    res = backend.start(tenant_opts("bithuman", "agent_code_1"))
    backend._tenant_keys.clear()
    try:
        backend.stop(res.session_id)
    except LemonSliceError:
        pass
    backend.stop_all()
    assert rest.stage("/v1/runtime-sessions/bh-1/end") == []
