"""static_avatar: the free test avatar publishes the avatar identity and plays TTS in order."""

import asyncio

import pytest

from backend.application.clients.avatar.static_avatar import (
    StaticAvatarRenderBackend,
    StaticAvatarSettings,
    render_frame,
)
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow
from backend.config import AppConfig


class FakeRoom:
    def __init__(self):
        self.connected = None
        self.disconnected = False

    async def connect(self, url, token):
        self.connected = (url, token)

    async def disconnect(self):
        self.disconnected = True


class FakeAudio:
    def __init__(self):
        self.frames = []
        self.cleared = 0
        self.playouts = 0

    async def capture(self, pcm):
        self.frames.append(pcm)

    def clear_queue(self):
        self.cleared += 1

    async def wait_for_playout(self):
        self.playouts += 1


def settings(**kw):
    return StaticAvatarSettings(
        livekit_url="wss://lk.example", livekit_api_key="k", livekit_api_secret="s" * 32, **kw
    )


def make():
    room, audio, pumps = FakeRoom(), FakeAudio(), []

    async def media(_room, _s, frame):
        assert len(frame) == _s.width * _s.height * 4  # RGBA

        async def pump():
            pumps.append(1)
            await asyncio.sleep(3600)

        return audio, pump

    backend = StaticAvatarRenderBackend(
        settings(width=64, height=36), room_factory=lambda: room, media_factory=media
    )
    return backend, room, audio, pumps


def win(uid, seq, ms=300, rate=24000, final=False):
    return AudioWindow(
        session_id="s",
        utterance_id=uid,
        seq=seq,
        sample_rate=rate,
        duration_ms=ms,
        pcm=b"\x01\x00" * (rate * ms // 1000),
        is_final=final,
    )


@pytest.fixture
def started():
    backend, room, audio, pumps = make()
    result = backend.start(StartOptions())
    yield backend, room, audio, pumps, result.session_id
    backend.stop_all()


def test_start_joins_as_avatar_identity_and_publishes(started):
    backend, room, audio, pumps, sid = started
    assert room.connected[0] == "wss://lk.example"
    assert backend.session_status(sid) == "active"
    assert pumps == [1]


def test_audio_is_chunked_and_final_waits_for_playout(started):
    backend, _room, audio, _pumps, sid = started
    backend.stream_audio(sid, win("u1", 0))
    backend.stream_audio(sid, win("u1", 1, final=True))
    assert b"".join(audio.frames) == b"\x01\x00" * (24000 * 600 // 1000)
    assert audio.playouts == 1
    assert backend.playback_info(sid, "u1")["playback_started_at"] is not None


def test_interrupt_drops_late_windows_of_that_utterance(started):
    backend, _room, audio, _pumps, sid = started
    backend.stream_audio(sid, win("u1", 0))
    sent = len(audio.frames)
    backend.interrupt(sid)
    backend.stream_audio(sid, win("u1", 1, final=True))
    assert audio.cleared == 1 and len(audio.frames) == sent and audio.playouts == 0


def test_stop_disconnects_and_forgets_session(started):
    backend, room, _audio, _pumps, sid = started
    backend.stop(sid)
    assert room.disconnected
    with pytest.raises(KeyError):
        backend.session_status(sid)


def test_render_frame_letterboxes_placeholder():
    assert len(render_frame(settings(width=32, height=18))) == 32 * 18 * 4


def _env(monkeypatch, **extra):
    for k, v in {
        "RENDER_BACKEND": "static_avatar",
        "LIVEKIT_URL": "wss://lk.example",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s" * 32,
        **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_config_builds_static_backend(monkeypatch):
    _env(monkeypatch, APP_ENV="dev")
    assert AppConfig.from_env().build_render_backend().name == "static_avatar"


def test_config_refuses_static_avatar_in_production(monkeypatch):
    _env(monkeypatch, APP_ENV="prod")
    with pytest.raises(ValueError, match="testing only"):
        AppConfig.from_env().build_render_backend()


def test_config_refuses_second_audio_publisher(monkeypatch):
    _env(monkeypatch, APP_ENV="dev", LIVEKIT_PUBLISH="1")
    with pytest.raises(ValueError, match="LIVEKIT_PUBLISH"):
        AppConfig.from_env().build_render_backend()
