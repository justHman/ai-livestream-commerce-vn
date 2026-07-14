"""Offline tests for the real LiveKit RTC publish path (fake transport seam).

We never import livekit-rtc here. The AudioTrackPublisher accepts an
``rtc_factory`` test seam that builds a fake room object with the shape
the real SDK exposes: ``local_participant.publish_track`` + a track with
``capture_frame``. This proves the publish path wires PCM -> AudioFrame ->
track without the SDK installed.
"""

from __future__ import annotations

import pytest

from core.livekit_publish import AudioTrackPublisher


class _FakeTrack:
    def __init__(self) -> None:
        self.frames = []

    def capture_frame(self, frame) -> None:
        self.frames.append(frame)


class _FakeParticipant:
    def __init__(self, track: _FakeTrack) -> None:
        self._track = track
        self.published = False

    async def publish_track(self, track, options=None) -> str:
        self.published = True
        return "track-id"


class _FakeRoom:
    def __init__(self) -> None:
        self.track = _FakeTrack()
        self.local_participant = _FakeParticipant(self.track)
        self.connected = False
        self.disconnected = False

    async def __aenter__(self):
        self.connected = True
        return self

    async def __aexit__(self, *exc):
        self.disconnected = True
        return False


def _factory(room_holder):
    def make(url, token):
        room_holder["room"] = _FakeRoom()
        return room_holder["room"]

    return make


@pytest.mark.asyncio
async def test_publish_pcm_routes_to_track_via_factory():
    env = {
        "LIVEKIT_PUBLISH": "1",
        "LIVEKIT_URL": "ws://lk:7880",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
    }
    holder: dict = {}
    pub = AudioTrackPublisher("sess", env=env, rtc_factory=_factory(holder))
    assert pub.enabled is True

    await pub.start()
    assert holder["room"].connected is True
    assert holder["room"].local_participant.published is True

    await pub.publish_pcm(b"\x00\x01" * 480, sample_rate=24000)
    await pub.publish_pcm(b"\x02\x03" * 480, sample_rate=24000)
    assert pub.frames_published == 2
    assert len(holder["room"].track.frames) == 2

    await pub.stop()
    assert holder["room"].disconnected is True


@pytest.mark.asyncio
async def test_publish_start_fails_loud_when_sdk_missing(monkeypatch):
    """When rtc_factory is None AND livekit import fails, start() raises, not silent no-op."""
    env = {
        "LIVEKIT_PUBLISH": "1",
        "LIVEKIT_URL": "ws://lk:7880",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
    }
    import sys

    monkeypatch.setitem(sys.modules, "livekit.rtc", None)  # force ImportError on import
    pub = AudioTrackPublisher("sess", env=env, rtc_factory=None)
    with pytest.raises(RuntimeError, match="livekit-rtc"):
        await pub.start()
