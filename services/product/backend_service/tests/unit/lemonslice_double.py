"""LemonSlice protocol double for P0-FB-010 (test double, never a real provider).

A fake ``rtc.Room`` plus a fake avatar participant. It records every data-stream
write, RPC and track event with timestamps, and implements the avatar side of
``lk.playback_started`` / ``lk.playback_finished`` / ``lk.clear_buffer``.
Modes: ``normal`` | ``video_only`` (the avatar publishes no audio) | ``never``
(the avatar never joins) | ``silent`` (never sends playback_finished).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import jwt

AVATAR = "lemonslice-avatar-agent"
KIND_VIDEO, KIND_AUDIO = 2, 1


@dataclass
class Event:
    kind: str
    at: float
    detail: Any = None


@dataclass
class Stream:
    attributes: dict
    destination: list
    chunks: list = field(default_factory=list)
    closed_reason: str | None = "open"
    opened_at: float = 0.0

    @property
    def pcm(self) -> bytes:
        return b"".join(c for _, c in self.chunks)


class _Writer:
    def __init__(self, stream: Stream, room: "FakeRoom") -> None:
        self.s, self.room = stream, room

    async def write(self, data: bytes) -> None:
        if not self.s.chunks:
            self.room.log("first_write", self.s.attributes["livento.utterance_id"])
            self.room.avatar_started()
        self.s.chunks.append((time.monotonic(), bytes(data)))

    async def aclose(self, *, reason: str | None = None) -> None:
        self.s.closed_reason = reason
        self.room.log("stream_closed", reason)
        if reason is None:
            self.room.avatar_plays(self.s)


class _Local:
    def __init__(self, room: "FakeRoom") -> None:
        self.room, self.rpc = room, {}

    def register_rpc_method(self, name, handler):
        self.rpc[name] = handler

    async def stream_bytes(self, name, *, topic="", attributes=None, destination_identities=None, **kw):
        assert topic == "lk.audio_stream"
        s = Stream(dict(attributes or {}), list(destination_identities or []), opened_at=time.monotonic())
        self.room.streams.append(s)
        return _Writer(s, self.room)

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=None):
        self.room.log("rpc", (destination_identity, method))
        if method == "lk.clear_buffer" and self.room.mode != "silent":
            self.room.log("clear_buffer_done")
            self.room.fire("lk.playback_finished", '{"interrupted": true}')
        return ""

    async def publish_track(self, *a, **k):
        self.room.audio_track_published = True


class FakeRoom:
    def __init__(self, mode: str = "normal", play_delay: float = 0.05) -> None:
        self.mode, self.play_delay = mode, play_delay
        self.events: list[Event] = []
        self.streams: list[Stream] = []
        self.remote_participants: dict = {}
        self.local_participant = _Local(self)
        self.connected_token: dict | None = None
        self.disconnected = False
        self.audio_track_published = False
        self.captured: list[tuple[float, bytes]] = []

    def log(self, kind, detail=None):
        self.events.append(Event(kind, time.monotonic(), detail))

    def times(self, kind):
        return [e.at for e in self.events if e.kind == kind]

    async def connect(self, url, token):
        self.connected_token = jwt.decode(token, options={"verify_signature": False})
        self.log("connect")

    async def disconnect(self):
        self.disconnected = True
        self.log("disconnect")

    # -- avatar side ------------------------------------------------
    def avatar_joins(self):
        if self.mode == "never":
            return
        pubs = {"v": SimpleNamespace(kind=KIND_VIDEO)}
        if self.mode != "video_only":
            pubs["a"] = SimpleNamespace(kind=KIND_AUDIO)
        self.remote_participants[AVATAR] = SimpleNamespace(track_publications=pubs)

    def fire(self, method, payload="", caller=AVATAR):
        handler = self.local_participant.rpc[method]
        data = SimpleNamespace(caller_identity=caller, payload=payload)
        asyncio.ensure_future(handler(data))
        self.log(method)

    def avatar_started(self):
        self.fire("lk.playback_started")

    def avatar_plays(self, stream: Stream):
        if self.mode == "silent":
            return
        asyncio.get_event_loop().call_later(
            self.play_delay, lambda: self.fire("lk.playback_finished", '{"interrupted": false}')
        )


class FakeLemonSlice:
    """Fake REST endpoint; launching a session makes the avatar join the room."""

    def __init__(self, room: FakeRoom, status: int = 200) -> None:
        self.room, self.status, self.calls = room, status, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body))
        if self.status < 300 and body.get("properties"):
            self.room.avatar_joins()
        return self.status, {"session_id": "ls-1"} if self.status < 300 else {}
