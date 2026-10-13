"""static_avatar: the free test avatar publishes the avatar identity and plays TTS in order."""

import asyncio
import threading
import time

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


def test_capture_failure_leaves_no_playback_evidence(started):
    backend, _room, audio, _pumps, sid = started

    async def boom(_pcm):
        raise RuntimeError("capture failed")

    audio.capture = boom
    with pytest.raises(RuntimeError):
        backend.stream_audio(sid, win("u1", 0))
    info = backend.playback_info(sid, "u1")
    assert info == {"playback_started_at": None, "playback_unconfirmed": True}


def test_stop_failure_keeps_ownership_and_can_retry(started):
    backend, room, _audio, _pumps, sid = started
    calls = []

    async def failing():
        calls.append(1)
        raise OSError("down")

    real, room.disconnect = room.disconnect, failing
    with pytest.raises(OSError):
        backend.stop(sid)
    assert backend.session_status(sid) == "active"  # still owned
    room.disconnect = real
    backend.stop(sid)
    assert room.disconnected and len(calls) == 1


def test_dead_video_pump_is_reported():
    room, audio = FakeRoom(), FakeAudio()

    async def media(_room, _s, _frame):
        async def pump():
            raise RuntimeError("encoder died")

        return audio, pump

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=lambda: room, media_factory=media
    )
    sid = backend.start(StartOptions()).session_id
    deadline = time.time() + 3
    while backend.session_status(sid) == "active" and time.time() < deadline:
        time.sleep(0.01)
    assert backend.session_status(sid) == "video_failed"
    backend.stop_all()


class EventRoom(FakeRoom):
    """A room that records handlers like livekit's rtc.Room.on(event, callback)."""

    def __init__(self):
        super().__init__()
        self.handlers = {}

    def on(self, event, callback):
        self.handlers[event] = callback


def _backend_with(room):
    audio = FakeAudio()

    async def media(_room, _s, _frame):
        async def pump():
            await asyncio.sleep(3600)

        return audio, pump

    return StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=lambda: room, media_factory=media
    )


def test_unexpected_room_disconnect_is_logged_and_reported(caplog):
    room = EventRoom()
    backend = _backend_with(room)
    sid = backend.start(StartOptions()).session_id
    assert backend.session_status(sid) == "active"
    with caplog.at_level("ERROR"):
        room.handlers["disconnected"](7)  # livekit calls it on its own loop
    assert backend.session_status(sid) == "room_lost"
    assert "room disconnected" in caplog.text and sid in caplog.text
    backend.stop_all()


def test_our_own_stop_is_not_reported_as_a_lost_room(caplog):
    room = EventRoom()
    backend = _backend_with(room)
    sid = backend.start(StartOptions()).session_id
    sess = backend._sessions[sid]
    backend.stop(sid)
    with caplog.at_level("ERROR"):
        room.handlers["disconnected"](None)  # the event our own disconnect() triggers
    assert sess.room_lost is False
    assert "room disconnected" not in caplog.text


def test_startup_that_outlives_the_caller_is_cleaned_up():
    room = FakeRoom()

    async def slow_media(_room, _s, _frame):
        await asyncio.sleep(0.3)  # the publish finishes after the caller's deadline

        async def pump():
            await asyncio.sleep(3600)

        return FakeAudio(), pump

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16, connect_timeout_s=0.05),
        room_factory=lambda: room,
        media_factory=slow_media,
    )
    with pytest.raises(TimeoutError):
        backend.start(StartOptions())
    backend.stop_all()
    time.sleep(0.5)
    assert not backend._sessions and room.disconnected


def test_failed_startup_does_not_cancel_another_callers_startup():
    rooms, calls = [], []

    def room_factory():
        rooms.append(FakeRoom())
        return rooms[-1]

    async def media(_room, _s, _frame):
        calls.append(1)
        if len(calls) == 1:  # the first caller fails while the second is still publishing
            await asyncio.sleep(0.05)
            raise RuntimeError("publish failed")
        await asyncio.sleep(0.3)

        async def pump():
            await asyncio.sleep(3600)

        return FakeAudio(), pump

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=room_factory, media_factory=media
    )
    results = {}

    def go(name):
        try:
            results[name] = backend.start(StartOptions()).session_id
        except Exception as exc:
            results[name] = exc

    a = threading.Thread(target=go, args=("a",))
    a.start()
    time.sleep(0.02)
    b = threading.Thread(target=go, args=("b",))
    b.start()
    a.join(5)
    b.join(5)
    assert isinstance(results["a"], RuntimeError) and isinstance(results["b"], str)
    assert not rooms[1].disconnected and results["b"] in backend._sessions
    backend.stop_all()


def test_failed_startup_cleanup_is_retried_by_stop_all():
    room, attempts = FakeRoom(), []

    async def flaky_disconnect():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("down")
        room.disconnected = True

    room.disconnect = flaky_disconnect

    async def media(_room, _s, _frame):
        raise RuntimeError("publish failed")

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=lambda: room, media_factory=media
    )
    with pytest.raises(RuntimeError):
        backend.start(StartOptions())
    assert not room.disconnected
    backend.stop_all()
    assert room.disconnected and len(attempts) == 2


def test_stop_all_cancels_a_startup_the_loop_has_not_registered_yet():
    room = FakeRoom()

    async def media(_room, _s, _frame):
        async def pump():
            await asyncio.sleep(3600)

        return FakeAudio(), pump

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=lambda: room, media_factory=media
    )
    loop = backend._ensure_loop()
    release = threading.Event()
    asyncio.run_coroutine_threadsafe(_block(release), loop)  # the loop is busy
    result = {}

    def go():
        try:
            result["r"] = backend.start(StartOptions())
        except BaseException as exc:
            result["r"] = exc

    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.1)  # start() has submitted its startup; the loop has not run it
    release_later = threading.Timer(0.2, release.set)
    release_later.start()
    backend.stop_all()
    t.join(5)
    time.sleep(0.2)
    assert not backend._sessions and not isinstance(result["r"], type(None))
    assert not hasattr(result["r"], "session_id")


async def _block(release):
    release.wait(5)  # blocks the whole loop on purpose: nothing else can run


def test_cancellation_during_startup_cleanup_keeps_the_room_owned():
    room, calls = FakeRoom(), []

    async def hanging_disconnect():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.Event().wait()  # never returns: only a cancellation ends it
        room.disconnected = True

    room.disconnect = hanging_disconnect

    async def media(_room, _s, _frame):
        raise RuntimeError("publish failed")

    backend = StaticAvatarRenderBackend(
        settings(width=16, height=16), room_factory=lambda: room, media_factory=media
    )
    t = threading.Thread(target=lambda: _swallow(backend.start, StartOptions()))
    t.start()
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    backend.stop_all()  # cancels the startup while it is inside disconnect
    t.join(5)
    assert room.disconnected and len(calls) == 2 and not backend._orphans


def _swallow(fn, *args):
    try:
        fn(*args)
    except BaseException:
        pass
