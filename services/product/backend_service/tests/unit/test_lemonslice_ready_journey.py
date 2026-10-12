"""Real keepalive loop against a provider double enforcing S2's 30s idle timeout."""

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from backend.application.clients.avatar.lemonslice import LemonSliceRenderBackend
from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.render.engines_base import StartOptions
from .lemonslice_double import AVATAR, FakeLemonSlice, FakeRoom
from .test_lemonslice_backend import settings, win
from .test_lemonslice_races import until


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waiting = threading.Event()
        self.ticks = asyncio.Queue()

    async def wait(self):
        self.waiting.set()
        await self.ticks.get()

    def advance(self, backend, provider, seconds=20, *, expired=False):
        assert self.waiting.wait(3)
        self.waiting.clear()
        self.now += seconds
        provider.expire()
        backend._loop.call_soon_threadsafe(self.ticks.put_nowait, None)
        if not expired:
            assert self.waiting.wait(3)  # next sleep proves the entire control tick completed


class IdleProvider(FakeLemonSlice):
    def __init__(self, room, clock):
        super().__init__(room)
        self.clock, self.deadline = clock, 0.0
        self.resets = []

    def client_factory(self):
        client = super().client_factory()
        start = client.start_agent_session

        async def create(**kw):
            sid = await start(**kw)
            self.deadline = self.clock.now + kw["idle_timeout"]
            return sid

        client.start_agent_session = create
        return client

    def expire(self):
        if self.clock.now >= self.deadline:
            self.room.remote_participants.pop(AVATAR, None)

    def __call__(self, url, headers, body, timeout):
        self.expire()
        if body["event"] == "reset-idle-timeout":
            if AVATAR not in self.room.remote_participants:
                return 404, {}
            self.deadline = self.clock.now + 30
            self.resets.append(self.clock.now - 1000)
        return super().__call__(url, headers, body, timeout)


def journey_backend():
    clock, room = Clock(), FakeRoom()
    provider = IdleProvider(room, clock)
    backend = LemonSliceRenderBackend(
        settings(idle_timeout_s=30, keepalive_s=20, rollover_after_s=1260, ready_ttl_s=1200),
        room_factory=lambda: room,
        session_client_factory=provider.client_factory,
        http_post=provider,
        monotonic=lambda: clock.now,
    )
    backend._wait_keepalive = clock.wait
    return backend, room, provider, clock


def snapshot(room, elapsed, phase):
    avatar = room.remote_participants[AVATAR]
    pubs = avatar.track_publications
    assert pubs["v"].kind == 2 and pubs["a"].kind == 1
    return {
        "elapsed_s": elapsed,
        "phase": phase,
        "participants": [
            {
                "identity": AVATAR,
                "sid": avatar.sid,
                "tracks": [{"sid": "v", "type": 1}, {"sid": "a", "type": 0}],
            }
        ],
    }


def test_silent_preparation_media_join_ready_wait_golive_first_utterance():
    backend, room, provider, clock = journey_backend()
    sid = backend.start(StartOptions()).session_id
    evidence = []
    try:
        assert backend._sessions[sid].phase == "preparing"
        for _ in range(7):  # 140s silent preparation exceeds both idle30s and observed110s
            clock.advance(backend, provider)
            assert AVATAR in room.remote_participants
        evidence.append(snapshot(room, 140, "preparing_media_join"))
        assert not room.streams and len(provider.starts) == 1
        backend.set_session_phase(sid, "ready")
        for _ in range(12):  # operator waits another four minutes before GoLive
            clock.advance(backend, provider)
            assert AVATAR in room.remote_participants
        evidence.append(snapshot(room, 380, "ready_before_golive"))
        backend.set_session_phase(sid, "warming")  # actual execution phase after opening start
        for _ in range(6):  # two more silent minutes while GoLive waits for opening
            clock.advance(backend, provider)
            assert AVATAR in room.remote_participants
        evidence.append(snapshot(room, 500, "golive_before_first_utterance"))
        assert not room.streams and len(provider.starts) == 1
        backend.stream_audio(sid, win("opening", 0, final=True))
        evidence.append(snapshot(room, 500, "first_utterance_complete"))
        assert room.streams[0].pcm and len(provider.starts) == 1
        assert provider.resets == list(range(20, 501, 20))
        assert {s["participants"][0]["sid"] for s in evidence} == {"PA_1"}
        if path := os.getenv("LS_READY_EVIDENCE"):
            Path(path).write_text(
                json.dumps({"snapshots": evidence, "resets_s": provider.resets}, indent=2),
                encoding="utf-8",
            )
    finally:
        backend.stop_all()


@pytest.mark.parametrize("phase", ["preparing", "ready"])
def test_ttl_stops_reset_at_deadline_even_before_lifecycle_poll(phase):
    backend, room, provider, clock = journey_backend()
    sid = backend.start(StartOptions()).session_id
    backend.set_session_phase(sid, phase)
    try:
        for _ in range(59):
            clock.advance(backend, provider)
        assert AVATAR in room.remote_participants
        assert len(provider.resets) == 59
        clock.advance(backend, provider, expired=True)
        assert until(lambda: backend._sessions[sid].keepalive.done())
        assert len(provider.resets) == 59 and len(provider.starts) == 1
        assert backend.lifecycle_status(sid, 1200) == "RUNTIME_READY_TTL_EXPIRED"
        with pytest.raises(LemonSliceError, match="RUNTIME_READY_TTL_EXPIRED"):
            backend.stream_audio(sid, win("too-late", 0, final=True))
        assert not room.streams and not backend._sessions[sid].has_streamed
        clock.now += 30
        provider.expire()
        assert AVATAR not in room.remote_participants
    finally:
        backend.stop_all()


def test_idle_provider_really_removes_unprotected_avatar_after_30_seconds():
    backend, room, provider, clock = journey_backend()
    backend.start(StartOptions())
    try:
        clock.now += 31  # intentionally omit the keepalive tick: counterfactual regression
        provider.expire()
        assert AVATAR not in room.remote_participants
    finally:
        backend.stop_all()
