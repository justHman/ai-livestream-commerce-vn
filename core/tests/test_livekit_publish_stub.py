"""Offline tests for LiveKit audio publish stub."""

from __future__ import annotations

import pytest

from core.livekit_publish import AudioTrackPublisher, publish_enabled


def test_publish_disabled_without_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIVEKIT_PUBLISH", raising=False)
    monkeypatch.setenv("LIVEKIT_URL", "ws://lk:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    assert publish_enabled() is False


def test_publish_disabled_without_creds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVEKIT_PUBLISH", "1")
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    assert publish_enabled() is False


def test_publish_enabled_with_flag_and_creds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVEKIT_PUBLISH", "1")
    monkeypatch.setenv("LIVEKIT_URL", "ws://lk:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    assert publish_enabled() is True


@pytest.mark.asyncio
async def test_publisher_noop_without_env():
    pub = AudioTrackPublisher("sess-1", env={})
    assert pub.enabled is False
    await pub.start()
    assert pub.started is False
    await pub.publish_pcm(b"\x00\x00", sample_rate=24000)
    assert pub.frames_published == 0
    await pub.stop()
    assert pub.started is False


@pytest.mark.asyncio
async def test_publisher_stub_path_when_enabled():
    env = {
        "LIVEKIT_PUBLISH": "1",
        "LIVEKIT_URL": "ws://lk:7880",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
    }
    pub = AudioTrackPublisher("sess-2", env=env)
    assert pub.enabled is True
    await pub.start()
    assert pub.started is True
    await pub.publish_pcm(b"\x01\x02", sample_rate=16000)
    await pub.publish_pcm(b"\x03\x04", sample_rate=16000)
    assert pub.frames_published == 2
    await pub.stop()
    assert pub.started is False
