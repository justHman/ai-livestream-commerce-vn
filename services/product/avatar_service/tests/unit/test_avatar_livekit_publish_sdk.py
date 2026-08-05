"""Offline tests for the LiveKit publisher through its fake transport seam."""

from __future__ import annotations

import asyncio

import pytest

from avatar.publishing import AudioTrackPublisher


# ruff: noqa: I001


_ENV = {
    "LIVEKIT_PUBLISH": "1",
    "LIVEKIT_URL": "ws://lk:7880",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "test-secret-32-characters-harmless",
}


class _FakeTrack:
    def __init__(self, capture_error: BaseException | None = None) -> None:
        self.frames = []
        self.capture_error = capture_error

    async def capture_frame(self, frame) -> None:
        if self.capture_error is not None:
            raise self.capture_error
        self.frames.append(frame)


class _FakeParticipant:
    def __init__(
        self,
        publish_error: BaseException | None = None,
        *,
        wait_for_release: bool = False,
    ) -> None:
        self.publish_error = publish_error
        self.published = 0
        self.publish_started = asyncio.Event()
        self.publish_release = asyncio.Event()
        self.wait_for_release = wait_for_release
        if not wait_for_release:
            self.publish_release.set()

    async def publish_track(self, track, options=None) -> str:
        self.publish_started.set()
        if self.publish_error is not None:
            raise self.publish_error
        if self.wait_for_release:
            await self.publish_release.wait()
        self.published += 1
        return "track-id"


class _FakeRoom:
    def __init__(
        self,
        *,
        publish_error: BaseException | None = None,
        capture_error: BaseException | None = None,
        disconnect_error: BaseException | None = None,
        wait_for_publish: bool = False,
        malformed: bool = False,
    ) -> None:
        self.track = _FakeTrack(capture_error)
        if not malformed:
            self.local_participant = _FakeParticipant(
                publish_error,
                wait_for_release=wait_for_publish,
            )
        self.connected = 0
        self.disconnected = 0
        self.disconnect_error = disconnect_error

    async def __aenter__(self):
        self.connected += 1
        return self

    async def __aexit__(self, *exc):
        self.disconnected += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        return False


class _CancellableDisconnectRoom(_FakeRoom):
    def __init__(self) -> None:
        super().__init__()
        self.disconnect_started = asyncio.Event()
        self.disconnect_release = asyncio.Event()
        self.disconnect_calls = 0

    async def __aexit__(self, *exc):
        self.disconnect_calls += 1
        self.disconnect_started.set()
        if self.disconnect_calls == 1:
            await self.disconnect_release.wait()
            raise asyncio.CancelledError
        self.disconnected += 1
        return False


class _CancellableDisconnectFactory:
    def __init__(self) -> None:
        self.rooms = []

    def __call__(self, url, token):
        room = _CancellableDisconnectRoom()
        self.rooms.append(room)
        return room


class _MalformedRoom:
    def __init__(self) -> None:
        self.track = _FakeTrack()
        self.connected = 0
        self.disconnected = 0

    async def __aenter__(self):
        self.connected += 1
        return self

    async def __aexit__(self, *exc):
        self.disconnected += 1
        return False


class _MalformedRoomFactory:
    def __init__(self) -> None:
        self.rooms = []
        self.malformed = True

    def __call__(self, url, token):
        room = _MalformedRoom() if self.malformed else _FakeRoom()
        self.rooms.append(room)
        return room


class _RoomFactory:
    def __init__(self, **room_kwargs) -> None:
        self.rooms = []
        self.room_kwargs = room_kwargs

    def __call__(self, url, token):
        room = _FakeRoom(**self.room_kwargs)
        room.url = url
        room.token = token
        self.rooms.append(room)
        return room


def _publisher(factory: _RoomFactory) -> AudioTrackPublisher:
    return AudioTrackPublisher("sess", env=_ENV, rtc_factory=factory)


@pytest.mark.asyncio
async def test_publish_pcm_routes_pcm_and_format_to_fake_track():
    factory = _RoomFactory()
    pub = _publisher(factory)

    await pub.start()
    await pub.publish_pcm(b"\x00\x01" * 480)
    await pub.stop()

    room = factory.rooms[0]
    assert room.connected == 1
    assert room.local_participant.published == 1
    assert room.track.frames == [
        {"pcm": b"\x00\x01" * 480, "sample_rate": 24_000, "num_channels": 1}
    ]
    assert room.disconnected == 1


@pytest.mark.asyncio
async def test_concurrent_start_connects_and_publishes_once():
    factory = _RoomFactory(wait_for_publish=True)
    pub = _publisher(factory)
    first = asyncio.create_task(pub.start())

    while not factory.rooms:
        await asyncio.sleep(0)
    participant = factory.rooms[0].local_participant
    await participant.publish_started.wait()
    second = asyncio.create_task(pub.start())
    await asyncio.sleep(0)
    assert not second.done()

    participant.publish_release.set()
    await asyncio.gather(first, second)

    assert len(factory.rooms) == 1
    assert factory.rooms[0].connected == 1
    assert factory.rooms[0].local_participant.published == 1


@pytest.mark.asyncio
async def test_stop_cancellation_completes_disconnect_and_reraises():
    factory = _CancellableDisconnectFactory()
    pub = _publisher(factory)
    await pub.start()
    room = factory.rooms[0]
    stop_task = asyncio.create_task(pub.stop())
    await room.disconnect_started.wait()
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    assert pub.started is False
    assert pub._room_ctx is None
    assert room.disconnected == 1
    assert room.disconnect_calls == 2

    await pub.stop()
    assert room.disconnect_calls == 2


@pytest.mark.asyncio
async def test_direct_start_binds_default_format_and_rejects_mismatch_before_capture():
    factory = _RoomFactory()
    pub = _publisher(factory)

    await pub.start()
    with pytest.raises(ValueError, match="expected 24000Hz/1ch"):
        await pub.publish_pcm(b"\x00\x01" * 480, sample_rate=16_000)

    assert factory.rooms[0].track.frames == []


@pytest.mark.asyncio
async def test_first_valid_pcm_binds_native_format_before_connecting():
    factory = _RoomFactory()
    pub = _publisher(factory)

    await pub.publish_pcm(b"\x00\x01" * 320, sample_rate=16_000)

    assert factory.rooms[0].track.frames[0]["sample_rate"] == 16_000
    assert factory.rooms[0].track.frames[0]["num_channels"] == 1


@pytest.mark.asyncio
async def test_invalid_pcm_does_not_connect():
    factory = _RoomFactory()
    pub = _publisher(factory)

    with pytest.raises(ValueError, match="complete interleaved"):
        await pub.publish_pcm(b"\x00")

    assert factory.rooms == []


@pytest.mark.asyncio
async def test_start_failure_disconnects_and_is_restartable():
    factory = _RoomFactory(publish_error=RuntimeError("publish failed"))
    pub = _publisher(factory)

    with pytest.raises(RuntimeError, match="publish failed"):
        await pub.start()

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1

    factory.room_kwargs = {}
    await pub.start()
    assert pub.started is True
    assert len(factory.rooms) == 2


@pytest.mark.asyncio
async def test_malformed_room_fails_start_cleans_resource_and_is_restartable():
    factory = _MalformedRoomFactory()
    pub = _publisher(factory)

    with pytest.raises(RuntimeError, match="local participant publish_track"):
        await pub.start()

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1

    factory.malformed = False
    await pub.start()
    assert pub.started is True


@pytest.mark.asyncio
async def test_capture_failure_disconnects_and_reraises_original_error():
    factory = _RoomFactory(capture_error=RuntimeError("capture failed"))
    pub = _publisher(factory)

    with pytest.raises(RuntimeError, match="capture failed"):
        await pub.publish_pcm(b"\x00\x01" * 480)

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1


@pytest.mark.asyncio
async def test_capture_failure_allows_new_first_format_after_cleanup():
    factory = _RoomFactory(capture_error=RuntimeError("capture failed"))
    pub = _publisher(factory)

    with pytest.raises(RuntimeError, match="capture failed"):
        await pub.publish_pcm(b"\x00\x01" * 320, sample_rate=16_000)

    factory.room_kwargs = {}
    await pub.publish_pcm(b"\x00\x01" * 480, sample_rate=24_000)

    assert factory.rooms[1].track.frames[0]["sample_rate"] == 24_000
    assert pub.started is True


@pytest.mark.asyncio
async def test_stop_failure_preserves_audio_format_for_retry():
    factory = _RoomFactory(disconnect_error=RuntimeError("disconnect failed"))
    pub = _publisher(factory)
    await pub.start()

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await pub.stop()

    with pytest.raises(ValueError, match="expected 24000Hz/1ch"):
        await pub.publish_pcm(b"\x00\x01" * 320, sample_rate=16_000)

    factory.rooms[0].disconnect_error = None
    await pub.stop()


@pytest.mark.asyncio
async def test_capture_cancellation_disconnects_and_reraises_cancellation():
    factory = _RoomFactory(capture_error=asyncio.CancelledError())
    pub = _publisher(factory)

    with pytest.raises(asyncio.CancelledError):
        await pub.publish_pcm(b"\x00\x01" * 480)

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1


@pytest.mark.asyncio
async def test_start_cancellation_disconnects_and_reraises_cancellation():
    factory = _RoomFactory(wait_for_publish=True)
    pub = _publisher(factory)
    task = asyncio.create_task(pub.start())
    while not factory.rooms:
        await asyncio.sleep(0)
    await factory.rooms[0].local_participant.publish_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1


@pytest.mark.asyncio
async def test_stop_serializes_with_capture_and_is_idempotent():
    factory = _RoomFactory()
    pub = _publisher(factory)
    await pub.start()

    capture_started = asyncio.Event()
    capture_release = asyncio.Event()

    async def capture(frame):
        capture_started.set()
        await capture_release.wait()
        factory.rooms[0].track.frames.append(frame)

    factory.rooms[0].track.capture_frame = capture
    publish_task = asyncio.create_task(pub.publish_pcm(b"\x00\x01" * 480))
    await capture_started.wait()
    stop_task = asyncio.create_task(pub.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    capture_release.set()
    await publish_task
    await stop_task
    await pub.stop()

    assert factory.rooms[0].disconnected == 1


@pytest.mark.asyncio
async def test_stop_failure_preserves_retryable_state():
    factory = _RoomFactory(disconnect_error=RuntimeError("disconnect failed"))
    pub = _publisher(factory)
    await pub.start()

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await pub.stop()

    assert pub.started is True
    assert factory.rooms[0].disconnected == 1
    factory.rooms[0].disconnect_error = None

    await pub.stop()

    assert pub.started is False
    assert factory.rooms[0].disconnected == 2


@pytest.mark.asyncio
async def test_multiple_pcm_buffers_use_one_published_track():
    factory = _RoomFactory()
    pub = _publisher(factory)

    await pub.publish_pcm(b"\x00\x01" * 480)
    await pub.publish_pcm(b"\x02\x03" * 480)

    assert pub.frames_published == 2
    assert len(factory.rooms) == 1
    assert len(factory.rooms[0].track.frames) == 2


@pytest.mark.asyncio
async def test_format_mismatch_is_rejected_before_capture():
    factory = _RoomFactory()
    pub = _publisher(factory)
    await pub.publish_pcm(b"\x00\x01" * 480)

    with pytest.raises(ValueError, match="PCM format mismatch"):
        await pub.publish_pcm(b"\x00\x01" * 480, num_channels=2)

    assert len(factory.rooms[0].track.frames) == 1


@pytest.mark.asyncio
async def test_disconnect_failure_during_start_keeps_original_start_error():
    factory = _RoomFactory(
        publish_error=RuntimeError("publish failed"),
        disconnect_error=RuntimeError("disconnect failed"),
    )
    pub = _publisher(factory)

    with pytest.raises(RuntimeError, match="publish failed"):
        await pub.start()

    assert pub.started is False
    assert factory.rooms[0].disconnected == 1


@pytest.mark.asyncio
async def test_stop_waits_for_start_to_finish():
    factory = _RoomFactory(wait_for_publish=True)
    pub = _publisher(factory)
    start_task = asyncio.create_task(pub.start())

    while not factory.rooms:
        await asyncio.sleep(0)
    participant = factory.rooms[0].local_participant
    await participant.publish_started.wait()

    stop_task = asyncio.create_task(pub.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    participant.publish_release.set()
    await start_task
    await stop_task

    assert factory.rooms[0].disconnected == 1
