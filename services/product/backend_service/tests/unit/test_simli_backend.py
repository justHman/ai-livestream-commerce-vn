"""cloud_simli backend against protocol doubles (no Simli, LiveKit or other network)."""

from __future__ import annotations

import asyncio
import logging
import time

import jwt
import pytest

from backend.application.clients.avatar.lemonslice import (
    LemonSliceError,
    LemonSliceRenderBackend,
)
from backend.application.clients.avatar.simli import (
    SESSION_LENGTH_MARGIN_S,
    SimliRenderBackend,
    SimliSettings,
)
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow
from backend.application.usage_evidence.cost import cost_meter
from backend.config import AppConfig

from .simli_double import AVATAR, FakeRoom, FakeSimli

pytestmark = pytest.mark.timeout(30)

SIMLI_KEY = "simli-secret-key-123"
LK_SECRET = "lk-secret-value-456-padded-to-32-bytes"
FACE = "face-1234-abcd"


def settings(**kw) -> SimliSettings:
    base = dict(
        livekit_url="wss://lk.example.test",
        livekit_api_key="lk-key",
        livekit_api_secret=LK_SECRET,
        simli_api_key=SIMLI_KEY,
        face_id=FACE,
        avatar_identity=AVATAR,  # the room double's avatar identity
        ready_timeout_s=0.5,
        request_timeout_s=1.0,
    )
    return SimliSettings(**(base | kw))


def make(**kw):
    room = FakeRoom("normal", 0.05)
    rest = FakeSimli(room)
    backend = SimliRenderBackend(settings(**kw), room_factory=lambda: room, async_http_post=rest)
    return backend, room, rest


def win(uid, seq, ms=100, final=False):
    n = 16000 * ms // 1000
    return AudioWindow(
        session_id="s",
        utterance_id=uid,
        seq=seq,
        sample_rate=16000,
        duration_ms=ms,
        pcm=b"\x01\x00" * n,
        is_final=final,
    )


@pytest.fixture(autouse=True)
def _cost_provider():
    yield
    cost_meter.avatar_provider = "lemonslice"


@pytest.fixture
def started():
    backend, room, rest = make()
    res = backend.start(StartOptions())
    yield backend, room, rest, res
    backend.stop_all()


def test_token_body_has_required_fields_and_no_optional_trinity_fields(started):
    _, _, rest, _ = started
    (url, headers, body) = rest.stage("token")[0]
    assert url == "https://api.simli.ai/compose/token"
    assert headers == {"x-simli-api-key": SIMLI_KEY}
    assert body == {
        "faceId": FACE,
        "handleSilence": True,
        "maxSessionLength": 3600 + SESSION_LENGTH_MARGIN_S,
        "maxIdleTime": 120,
    }


def test_token_body_includes_configured_trinity_fields():
    backend, _, rest = make(
        streaming_width=1366,
        streaming_height=768,
        avatar_position_x=0.5,
        avatar_position_y=0.0,
        background_id="bg1",
        foreground_id="fg1",
        camera={"zoom": 1.2},
    )
    backend.start(StartOptions())
    body = rest.stage("token")[0][2]
    backend.stop_all()
    assert {k: body[k] for k in body if k not in {"faceId", "handleSilence"}} == {
        "maxSessionLength": 3660,
        "maxIdleTime": 120,
        "streaming_width": 1366,
        "streaming_height": 768,
        "avatar_position_x": 0.5,
        "avatar_position_y": 0.0,
        "background_id": "bg1",
        "foreground_id": "fg1",
        "camera": {"zoom": 1.2},
    }


def test_join_body_and_avatar_jwt_claims(started):
    _, _, rest, res = started
    (url, headers, body) = rest.stage("join")[0]
    assert url == "https://api.simli.ai/integrations/livekit/agents"
    assert "x-simli-api-key" not in headers
    assert set(body) == {"session_token", "livekit_token", "livekit_url"}
    assert body["session_token"] == "simli-session-token-xyz"
    assert body["livekit_url"] == "wss://lk.example.test"
    claims = jwt.decode(body["livekit_token"], options={"verify_signature": False})
    assert claims["kind"] == "agent" and claims["sub"] == AVATAR
    assert claims["attributes"] == {"lk.publish_on_behalf": "livento-runtime"}
    assert claims["video"]["room"] == res.session_id
    assert LK_SECRET not in str(claims)
    assert res.mode == "SIMLI"


def test_per_session_face_id_wins_over_default():
    backend, _, rest = make()
    backend.start(StartOptions(avatar_provider="simli", avatar_agent_id="other-face-99"))
    backend.stop_all()
    assert rest.stage("token")[0][2]["faceId"] == "other-face-99"


@pytest.mark.parametrize(
    "opts",
    [
        StartOptions(avatar_provider="lemonslice", avatar_agent_id="agent_0123456789ab"),
        StartOptions(avatar_agent_id="other-face-99"),  # no provider
        StartOptions(avatar_provider="simli", avatar_image_url="https://x.test/a.png"),
        StartOptions(avatar_provider="simli", avatar_agent_id="bad id!"),
        StartOptions(avatar_id="not-the-face"),
    ],
)
def test_invalid_avatar_selection_never_posts(opts):
    backend, _, rest = make()
    with pytest.raises(LemonSliceError):
        backend.start(opts)
    backend.stop_all()
    assert rest.calls == []


def test_missing_face_id_is_refused_without_post():
    backend, _, rest = make(face_id="")
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    backend.stop_all()
    assert err.value.code == "agent_not_configured" and rest.calls == []


@pytest.mark.parametrize("stage", ["token", "join"])
def test_start_failure_is_not_retried_and_leaves_no_session(stage):
    backend, room, rest = make()
    setattr(rest, f"{stage}_status", 500)
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    assert err.value.code == "session_request_failed" and "status=500" in str(err.value)
    assert len(rest.stage("token")) == 1 and len(rest.stage("join")) <= 1
    assert backend.session_ids() == () and room.disconnected
    backend.stop_all()


def test_token_failure_never_reaches_join():
    backend, _, rest = make()
    rest.token_status = 401
    with pytest.raises(LemonSliceError):
        backend.start(StartOptions())
    backend.stop_all()
    assert rest.stage("join") == []


def test_transport_error_message_has_no_url_or_key():
    backend, _, rest = make()
    rest.raise_on["token"] = ConnectionError(f"boom https://api.simli.ai/?k={SIMLI_KEY}")
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    backend.stop_all()
    assert SIMLI_KEY not in str(err.value) and "http" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_missing_session_token_or_id_fails_closed():
    backend, _, rest = make()
    rest.token_payload = {}
    with pytest.raises(LemonSliceError):
        backend.start(StartOptions())
    assert rest.stage("join") == []
    rest.token_payload = {"session_token": "t"}
    rest.join_payload = {"session_id": "bad id/../"}
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    backend.stop_all()
    assert err.value.code == "session_id_invalid"


def test_credit_exhausted_pauses_creation():
    backend, _, rest = make()
    rest.token_status = 402
    with pytest.raises(LemonSliceError) as first:
        backend.start(StartOptions())
    rest.token_status = 200
    with pytest.raises(LemonSliceError) as second:
        backend.start(StartOptions())
    backend.stop_all()
    assert first.value.code == second.value.code == "LEMONSLICE_CREDIT_EXHAUSTED"
    assert len(rest.stage("token")) == 1


def test_token_warnings_are_logged_without_secrets(caplog):
    backend, _, rest = make()
    rest.token_payload = {
        "session_token": "tok-secret-777",
        "warnings": [f"width clamped {SIMLI_KEY} tok-secret-777"],
    }
    with caplog.at_level(logging.WARNING):
        backend.start(StartOptions())
    backend.stop_all()
    assert "width clamped" in caplog.text
    assert SIMLI_KEY not in caplog.text and "tok-secret-777" not in caplog.text


def test_secrets_not_in_repr_or_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        backend, _, _ = make()
        backend.start(StartOptions())
        backend.stop_all()
    text = caplog.text + repr(settings())
    assert SIMLI_KEY not in text and LK_SECRET not in text


def test_ready_gate_waits_for_avatar_video(started):
    backend, room, _, res = started
    assert backend.speak_ready_gate_enabled and backend.avatar_ready(res.session_id)
    room.remote_participants.clear()
    assert not backend.avatar_ready(res.session_id)


def test_audio_is_sent_as_16k_pcm_over_the_data_stream(started):
    backend, room, _, res = started
    pcm = win("u1", 0, final=True).pcm
    backend.stream_audio(res.session_id, win("u1", 0, final=True))
    (stream,) = room.streams
    assert stream.destination == [AVATAR]
    assert stream.attributes["sample_rate"] == "16000" and stream.pcm == pcm


def test_stream_audio_yields_no_video_window(started):
    backend, _, _, res = started
    assert list(backend.stream_audio(res.session_id, win("u1", 0, final=True))) == []


def test_interrupt_sends_clear_buffer_and_drops_late_windows(started):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0))
    backend.interrupt(res.session_id)
    assert (AVATAR, "lk.clear_buffer") in [e.detail for e in room.events if e.kind == "rpc"]
    backend.stream_audio(res.session_id, win("u1", 1, final=True))
    assert len(room.streams) == 1


def test_stop_leaves_room_once_and_is_idempotent():
    backend, room, rest = make()
    res = backend.start(StartOptions())
    backend.stream_audio(res.session_id, win("u1", 0))
    backend.stop(res.session_id)
    assert room.streams[0].closed_reason == "interrupted" and room.disconnected
    backend.stop(res.session_id)
    backend.stop("never-existed")
    assert backend.session_ids() == ()
    backend.stop_all()
    assert [e.kind for e in room.events].count("disconnect") <= 2  # teardown + provider leave
    with pytest.raises(KeyError):
        backend.interrupt(res.session_id)


def test_stop_all_stops_every_session_and_is_safe_twice():
    backend, room, _ = make()
    backend.start(StartOptions())
    backend.stop_all()
    assert room.disconnected and backend.session_ids() == ()
    backend.stop_all()


def test_failed_room_leave_keeps_cleanup_pending_then_stop_retries():
    backend, room, _ = make()
    res = backend.start(StartOptions())
    room.fail["disconnect"] = [RuntimeError("leave failed")]
    with pytest.raises(LemonSliceError) as err:
        backend.stop(res.session_id)
    assert err.value.code == "provider_cleanup_incomplete"
    backend.stop(res.session_id)  # retry succeeds
    backend.stop_all()
    assert room.disconnected


def test_near_cap_ends_new_utterances_and_reports_status():
    backend, _, _ = make(max_session_s=0.3)
    res = backend.start(StartOptions())
    assert backend.session_status(res.session_id) == "active"
    time.sleep(0.4)
    assert backend.session_status(res.session_id) == "near_cap"
    with pytest.raises(LemonSliceError) as err:
        backend.stream_audio(res.session_id, win("u1", 0, final=True))
    backend.stop_all()
    assert err.value.code == "session_near_cap"


def test_recovery_flushes_buffer_instead_of_starting_a_second_session(started):
    backend, room, rest, res = started
    backend.require_avatar_recovery(res.session_id)
    assert backend.wait_speak_ready(res.session_id) is True
    assert len(rest.stage("token")) == 1
    assert (AVATAR, "lk.clear_buffer") in [e.detail for e in room.events if e.kind == "rpc"]


def test_cost_evidence_uses_simli_provider(monkeypatch):
    seen: list = []
    cost_meter.configure(enabled=True)
    monkeypatch.setattr(cost_meter, "_push", lambda *a, **k: seen.append(k))
    backend, _, _ = make()
    res = backend.start(StartOptions())
    backend.stop(res.session_id)
    backend.stop_all()
    assert [k["provider"] for k in seen] == ["simli"]
    assert seen[0]["usage_source"] == "derived"
    cost_meter.configure(enabled=False)


# ---- settings / config -------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        dict(streaming_width=768),  # height missing
        dict(streaming_width=256, streaming_height=256),  # < 512
        dict(streaming_width=800, streaming_height=600),  # 4:3
        dict(avatar_position_x=1.5),
        dict(max_idle_s=0),
        dict(max_session_s=0),
        dict(face_id="no spaces allowed"),
    ],
)
def test_settings_validation_refuses_bad_values(kw):
    with pytest.raises(ValueError):
        settings(**kw)


def test_default_identity_and_session_cap():
    s = SimliSettings("u", "k", "s", "key")
    assert s.avatar_identity == "simli-avatar-agent" and s.max_session_s == 3600


def _cfg(monkeypatch, **env):
    for k in ("RENDER_BACKEND", "AVATAR_ADAPTER", "LIVEKIT_PUBLISH", "TTS_VOICE_ID"):
        monkeypatch.delenv(k, raising=False)
    base = {
        "RENDER_BACKEND": "cloud_simli",
        "LIVEKIT_URL": "wss://lk.example.test",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
        "SIMLI_API_KEY": "x",
        "SIMLI_FACE_ID": FACE,
    }
    for k, v in (base | env).items():
        monkeypatch.setenv(k, v)
    return AppConfig.from_env()


def test_config_builds_simli_backend_and_is_a_lemonslice_subclass(monkeypatch):
    backend = _cfg(monkeypatch).build_render_backend()
    assert backend.name == "cloud_simli" and isinstance(backend, LemonSliceRenderBackend)
    backend.stop_all()


def test_config_missing_key_fails_closed(monkeypatch):
    with pytest.raises(ValueError, match="SIMLI_API_KEY"):
        _cfg(monkeypatch, SIMLI_API_KEY="").build_render_backend()


def test_config_refuses_second_audio_publisher(monkeypatch):
    with pytest.raises(ValueError, match="LIVEKIT_PUBLISH"):
        _cfg(monkeypatch, LIVEKIT_PUBLISH="true").build_render_backend()


def test_config_reads_optional_trinity_env(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        SIMLI_STREAMING_WIDTH="1366",
        SIMLI_STREAMING_HEIGHT="768",
        SIMLI_AVATAR_POSITION_X="0.4",
        SIMLI_CAMERA_JSON='{"zoom": 1}',
    )
    assert (cfg.simli_streaming_width, cfg.simli_avatar_position_x) == (1366, 0.4)
    cfg.build_render_backend().stop_all()


@pytest.mark.parametrize(
    "env",
    [{"SIMLI_STREAMING_WIDTH": "abc"}, {"SIMLI_AVATAR_POSITION_Y": "2"}, {"SIMLI_MAX_IDLE_S": "0"}],
)
def test_config_rejects_bad_env_without_echoing_value(monkeypatch, env):
    with pytest.raises(ValueError) as err:
        _cfg(monkeypatch, **env)
    assert next(iter(env.values())) not in str(err.value).replace(next(iter(env)), "")


def test_config_bad_camera_json_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="SIMLI_CAMERA_JSON"):
        _cfg(monkeypatch, SIMLI_CAMERA_JSON="[1]").build_render_backend()


def test_default_backend_unchanged_and_lemonslice_still_builds(monkeypatch):
    monkeypatch.delenv("RENDER_BACKEND", raising=False)
    monkeypatch.delenv("AVATAR_ADAPTER", raising=False)
    assert AppConfig.from_env().render_backend == "cloud_liveavatar"
    assert LemonSliceRenderBackend.name == "cloud_lemonslice"
    assert LemonSliceRenderBackend.start_mode == "LEMONSLICE"
    assert LemonSliceRenderBackend.cost_provider == "lemonslice"


def test_simli_start_does_not_need_the_lemonslice_plugin(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "livekit.plugins.lemonslice.api", None)
    backend, _, _ = make()
    backend.start(StartOptions())
    backend.stop_all()


def test_event_loop_is_not_leaked_between_backends():
    async def noop():
        return None

    backend, _, _ = make()
    backend.start(StartOptions())
    backend.stop_all()
    assert asyncio.run(noop()) is None
