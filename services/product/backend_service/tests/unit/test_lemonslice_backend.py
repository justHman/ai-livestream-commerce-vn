"""P0-FB-010 Runtime slice: cloud_lemonslice backend against a protocol double.

Every test uses ``lemonslice_double`` (fake LiveKit room, fake avatar, fake REST).
No LemonSlice, a TTS provider or LiveKit network is touched; keys are fake strings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest

from backend.application.clients.avatar.lemonslice import (
    LemonSliceRenderBackend,
    LemonSliceSettings,
)
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow
from backend.config import AppConfig

from . import test_approved_speech_active as speech_tests
from . import test_autonomous_start as start_tests
from .lemonslice_double import AVATAR, FakeLemonSlice, FakeRoom

case_factory = speech_tests.case_factory

pytestmark = pytest.mark.timeout(30)

LS_KEY = "ls-secret-key-123"
LK_SECRET = "lk-secret-value-456-padded-to-32-bytes"


def settings(**kw) -> LemonSliceSettings:
    base = dict(
        livekit_url="wss://lk.example.test",
        livekit_api_key="lk-key",
        livekit_api_secret=LK_SECRET,
        lemonslice_api_key=LS_KEY,
        agent_id="agent-preset",
        ready_timeout_s=0.5,
        playback_margin_s=0.3,
    )
    return LemonSliceSettings(**(base | kw))


def make(mode="normal", status=200, play_delay=0.05, **kw):
    room = FakeRoom(mode, play_delay)
    rest = FakeLemonSlice(room, status)

    def track_factory(_room, _rate):
        async def publish():
            room.audio_track_published = True

        def capture(pcm):
            room.captured.append((time.monotonic(), pcm))

        return publish, capture

    backend = LemonSliceRenderBackend(
        settings(**kw), room_factory=lambda: room, http_post=rest, audio_track_factory=track_factory
    )
    return backend, room, rest


def win(uid, seq, ms=100, rate=16000, final=False, sid="s"):
    n = rate * ms // 1000
    return AudioWindow(
        session_id=sid,
        utterance_id=uid,
        seq=seq,
        sample_rate=rate,
        duration_ms=ms,
        pcm=b"\x01\x00" * n,
        is_final=final,
    )


@pytest.fixture
def started():
    backend, room, rest = make()
    res = backend.start(StartOptions())
    yield backend, room, rest, res
    backend.stop_all()


def test_start_posts_single_speaker_body_and_scopes_avatar_token(started):
    backend, room, rest, res = started
    url, headers, body = rest.calls[0]
    assert url == "https://lemonslice.com/api/liveai/sessions"
    assert headers == {"X-API-Key": LS_KEY}
    assert set(body) == {"transport_type", "agent_id", "idle_timeout", "properties"}
    assert body["transport_type"] == "livekit" and body["agent_id"] == "agent-preset"
    avatar = jwt.decode(body["properties"]["livekit_token"], options={"verify_signature": False})
    assert avatar["kind"] == "agent" and avatar["sub"] == AVATAR
    assert avatar["attributes"] == {"lk.publish_on_behalf": "livento-runtime"}
    assert avatar["video"]["room"] == res.session_id == body["properties"]["livekit_session_id"]
    assert avatar["exp"] - avatar["nbf"] <= 300
    assert res.mode == "LEMONSLICE"


def test_runtime_participant_is_data_only_and_preview_token_is_subscribe_only(started):
    backend, room, rest, res = started
    grants = room.connected_token["video"]
    assert (grants["canPublish"], grants["canPublishData"]) == (False, True)
    preview = jwt.decode(res.livekit_client_token, options={"verify_signature": False})
    assert (preview["video"]["canPublish"], preview["video"]["canSubscribe"]) == (False, True)
    assert preview["video"]["canPublishData"] is False


@pytest.mark.parametrize("status", [401, 500])
def test_rest_error_fails_start_leaves_room_and_leaks_nothing(status):
    backend, room, _ = make(status=status)
    with pytest.raises(RuntimeError) as err:
        backend.start(StartOptions())
    assert room.disconnected
    assert LS_KEY not in str(err.value) and LK_SECRET not in str(err.value)


def test_avatar_never_joins_fails_after_bounded_wait_and_terminates():
    backend, room, rest = make(mode="never", terminate_path="/sessions/{session_id}/terminate")
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="video"):
        backend.start(StartOptions())
    assert time.monotonic() - t0 < 5
    assert room.disconnected
    assert rest.calls[-1][0].endswith("/sessions/ls-1/terminate")


def test_avatar_id_must_be_preset_or_allowlisted():
    backend, _, _ = make(avatar_allowlist=("other-preset",))
    with pytest.raises(RuntimeError, match="allowlist"):
        backend.start(StartOptions(avatar_id="custom-upload"))
    backend.start(StartOptions(avatar_id="other-preset"))
    backend.stop_all()


def test_one_stream_per_utterance_in_order_final_closes_only_avatar_receives(started):
    backend, room, _, res = started
    for i in range(3):
        assert list(backend.stream_audio(res.session_id, win("u1", i, final=(i == 2)))) == []
    assert len(room.streams) == 1
    s = room.streams[0]
    assert s.destination == [AVATAR]
    assert s.attributes["sample_rate"] == "16000" and s.attributes["livento.utterance_id"] == "u1"
    assert s.closed_reason is None  # closed normally
    assert [c for _, c in s.chunks] == [b"\x01\x00" * 1600] * 3  # 16 kHz: not resampled


def test_resampled_duration_matches_source_within_one_frame(started):
    backend, room, _, res = started
    for i in range(3):
        backend.stream_audio(res.session_id, win("u1", i, ms=200, rate=24000, final=(i == 2)))
    out_ms = len(room.streams[0].pcm) / 2 / 16000 * 1000
    assert abs(out_ms - 600) <= 20


def test_final_window_returns_only_after_playback_finished(started):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0, final=True))
    returned = time.monotonic()
    assert (
        room.times("lk.playback_finished")[0] <= returned
    )  # not a wall-clock guess (Windows timer skew)
    assert backend.playback_info(res.session_id, "u1")["playback_unconfirmed"] is False
    first_write = room.times("first_write")[0]
    assert (
        first_write <= room.times("lk.playback_started")[0] <= room.times("lk.playback_finished")[0]
    )


def test_unconfirmed_playback_is_bounded_and_recorded():
    backend, room, _ = make(mode="silent", playback_margin_s=0.2)
    res = backend.start(StartOptions())
    t0 = time.monotonic()
    backend.stream_audio(res.session_id, win("u1", 0, final=True))
    assert 0.2 <= time.monotonic() - t0 < 3
    assert backend.playback_info(res.session_id, "u1")["playback_unconfirmed"] is True
    backend.stop_all()


def test_playback_started_is_trusted_only_from_the_avatar(started):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0))
    intruder = SimpleNamespace(caller_identity="intruder", payload="")
    handler = room.local_participant.rpc["lk.playback_started"]
    asyncio.run_coroutine_threadsafe(handler(intruder), backend._ensure_loop()).result(2)
    stamp = backend.playback_info(res.session_id, "u1")["playback_started_at"]
    assert stamp is not None and len(room.times("lk.playback_started")) == 1
    assert backend.playback_info(res.session_id, "other") == {
        "playback_started_at": None,
        "playback_unconfirmed": False,
    }


def test_interrupt_aborts_stream_clears_buffer_and_drops_late_windows(started):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0))
    backend.interrupt(res.session_id)
    s = room.streams[0]
    assert s.closed_reason == "interrupted"
    assert (AVATAR, "lk.clear_buffer") in [e.detail for e in room.events if e.kind == "rpc"]
    written = len(s.chunks)
    backend.stream_audio(res.session_id, win("u1", 1, final=True))  # late window
    assert len(room.streams) == 1 and len(s.chunks) == written
    backend.stream_audio(res.session_id, win("u2", 0, final=True))  # next utterance is fine
    assert len(room.streams) == 2 and room.streams[1].attributes["livento.epoch"] == "1"


def test_no_audio_written_after_clear_buffer_completes(started):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0))
    backend.interrupt(res.session_id)
    done = room.times("clear_buffer_done")[0]
    assert all(t <= done for s in room.streams for t, _ in s.chunks)


def test_hold_sends_nothing(started):
    _, room, _, _ = started
    assert room.streams == []


def test_stop_clears_terminates_leaves_and_unknown_session_raises():
    backend, room, rest = make(terminate_path="/sessions/{session_id}/terminate")
    res = backend.start(StartOptions())
    backend.stream_audio(res.session_id, win("u1", 0))
    backend.stop(res.session_id)
    assert room.streams[0].closed_reason == "interrupted"
    assert room.disconnected and rest.calls[-1][0].endswith("/sessions/ls-1/terminate")
    with pytest.raises(KeyError):
        backend.stop(res.session_id)
    with pytest.raises(KeyError):
        backend.interrupt("nope")


def test_session_status_reports_avatar_presence(started):
    backend, room, _, res = started
    assert backend.session_status(res.session_id) == "active"
    room.remote_participants.clear()
    assert backend.session_status(res.session_id) == "avatar_absent"


def test_video_only_fallback_publishes_one_delayed_track():
    backend, room, _ = make(mode="video_only", fallback_publish=True, render_offset_ms=150)
    res = backend.start(StartOptions())
    assert room.connected_token["video"]["canPublish"] is True
    t0 = time.monotonic()
    backend.stream_audio(res.session_id, win("u1", 0, final=True))
    time.sleep(0.4)
    assert len(room.captured) == 1 and room.captured[0][1] == room.streams[0].pcm
    assert room.captured[0][0] - t0 >= 0.13  # 150 ms offset minus timer resolution
    backend.stop_all()


def test_default_publishes_no_runtime_audio_track():
    backend, room, _ = make(mode="video_only")
    backend.start(StartOptions())
    assert room.audio_track_published is False
    assert room.connected_token["video"]["canPublish"] is False
    backend.stop_all()


def test_secrets_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    bad, _, _ = make(status=500)
    with pytest.raises(RuntimeError):
        bad.start(StartOptions())
    ok, _, _ = make(mode="silent", playback_margin_s=0.1)
    res = ok.start(StartOptions())
    ok.stream_audio(res.session_id, win("u1", 0, final=True))
    ok.stop_all()
    text = caplog.text + repr(settings())
    assert LS_KEY not in text and LK_SECRET not in text


def _cfg(monkeypatch, **env):
    for k in ("RENDER_BACKEND", "AVATAR_ADAPTER", "LIVEKIT_PUBLISH", "TTS_VOICE_ID"):
        monkeypatch.delenv(k, raising=False)
    base = {
        "RENDER_BACKEND": "cloud_lemonslice",
        "LIVEKIT_URL": "wss://lk.example.test",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
        "LEMONSLICE_API_KEY": "x",
    }
    for k, v in (base | env).items():
        monkeypatch.setenv(k, v)
    return AppConfig.from_env()


def test_config_builds_lemonslice_backend(monkeypatch):
    assert _cfg(monkeypatch).build_render_backend().name == "cloud_lemonslice"


def test_avatar_adapter_lemonslice_maps_to_backend(monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.delenv("RENDER_BACKEND")
    monkeypatch.setenv("AVATAR_ADAPTER", "lemonslice")
    assert AppConfig.from_env().render_backend == "cloud_lemonslice"


def test_default_render_backend_stays_cloud_liveavatar(monkeypatch):
    monkeypatch.delenv("RENDER_BACKEND", raising=False)
    monkeypatch.delenv("AVATAR_ADAPTER", raising=False)
    assert AppConfig.from_env().render_backend == "cloud_liveavatar"


def test_config_refuses_second_audio_publisher(monkeypatch):
    with pytest.raises(ValueError, match="LIVEKIT_PUBLISH"):
        _cfg(monkeypatch, LIVEKIT_PUBLISH="true").build_render_backend()


@pytest.mark.parametrize(
    "missing", ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LEMONSLICE_API_KEY"]
)
def test_config_requires_credentials(monkeypatch, missing):
    with pytest.raises(ValueError, match=missing):
        _cfg(monkeypatch, **{missing: ""}).build_render_backend()


@pytest.mark.asyncio
async def test_orchestrator_completes_an_utterance_with_zero_video_windows():
    from backend.application.render.orchestrator import (
        StreamingControllerConfig,
        StreamOrchestrator,
    )
    from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
    from backend.application.text_chunker import ChunkPolicy, FixedChunkPolicyConfig

    from .test_orchestrator_tts_telemetry import _FixedTTS, _StubLLM

    backend, room, _ = make()
    res = await asyncio.to_thread(backend.start, StartOptions())
    queue = BoundedVideoQueue(max_size=4)
    orch = StreamOrchestrator(
        llm=_StubLLM(["Xin chào bạn."]),
        tts=_FixedTTS(),
        backend=backend,
        queue=queue,
        metrics=CoordinatorMetrics(),
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
        chunk_policy=ChunkPolicy.FIXED,
    )
    spoken = await asyncio.wait_for(orch.run(res.session_id, "hi"), timeout=10)
    assert spoken and queue.qsize() == 0
    assert len(room.streams) == 1 and room.streams[0].closed_reason is None
    assert room.streams[0].attributes["sample_rate"] == "16000"  # 24 kHz TTS resampled
    await asyncio.to_thread(backend.stop_all)


@pytest.mark.asyncio
async def test_execution_get_returns_additive_opening_media_with_playback_started_at(case_factory):
    case = await start_tests.prepared(case_factory)
    url = f"/api/v1/sessions/{case.sid}/execution"
    assert (await case.client.get(url)).json()["opening_media"] is None
    await start_tests.request(case)
    await start_tests.until(lambda: case.d.coordinator.opening_media(case.sid))
    receipt = case.d.coordinator.opening_media(case.sid)
    body = (await case.client.get(url)).json()
    assert body["opening_media"] == {**receipt, "playback_started_at": None}
    case.d.backend.playback_info = lambda sid, uid: {"playback_started_at": 1759226400.25}
    body = (await case.client.get(url)).json()
    assert body["opening_media"]["playback_started_at"] == "2025-09-30T10:00:00.250000Z"
    assert set(body) == {"state", "capabilities", "opening_media"}


@pytest.mark.parametrize("rate", [22050, 44100])
def test_whole_utterance_from_any_engine_rate_is_chunked_and_resampled(started, rate):
    backend, room, _, res = started
    backend.stream_audio(res.session_id, win("u1", 0, ms=1000, rate=rate, final=True))
    chunks = room.streams[0].chunks
    assert len(chunks) >= 5 and max(len(c) for _, c in chunks) <= 16000 // 5 * 2
    assert abs(len(room.streams[0].pcm) / 2 / 16000 - 1.0) <= 0.02


FIXTURE = Path(__file__).parent.parent / "fixtures" / "opening_media_playback_started.json"


@pytest.mark.asyncio
async def test_opening_media_bytes_match_shared_api_fixture(case_factory):
    """Same fixture file lives in the API repo (ai-connector usecase testdata); both sides decode/emit it."""
    fixture = json.loads(FIXTURE.read_text())
    case = await start_tests.prepared(case_factory)
    receipt = {k: v for k, v in fixture.items() if k != "playback_started_at"}
    case.d.coordinator.opening_media = lambda sid: dict(receipt)
    case.d.backend.playback_info = lambda sid, uid: {"playback_started_at": 1759226400.25}
    body = (await case.client.get(f"/api/v1/sessions/{case.sid}/execution")).json()
    assert body["opening_media"] == fixture


def test_resampler_fails_closed_when_livekit_rtc_is_missing(monkeypatch):
    """A rate change needs livekit-rtc; without it we raise, never pass 24 kHz audio off as 16 kHz."""
    import builtins

    from backend.application.publishing.datastream import UtteranceResampler

    real_import = builtins.__import__

    def no_livekit(name, *args, **kwargs):
        if name == "livekit" or name.startswith("livekit."):
            raise ImportError("No module named 'livekit'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_livekit)
    with pytest.raises(ImportError):
        UtteranceResampler(24000, 16000)
    assert (
        UtteranceResampler(16000, 16000).push(b"\x01\x00") == b"\x01\x00"
    )  # same rate needs no livekit
