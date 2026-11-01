"""Simli protocol double (test double, never a real provider).

Reuses the fake LiveKit room / avatar of ``lemonslice_double``. ``FakeSimli`` answers the two
HTTPS POSTs of a Simli start (``compose/token`` then ``integrations/livekit/agents``) as an
injectable ``async_http_post``; the join POST makes the avatar enter the room.
The room double's avatar identity is ``lemonslice_double.AVATAR``: tests configure that same
identity on the Simli settings.
"""

from __future__ import annotations

from typing import Any

from .lemonslice_double import AVATAR, FakeRoom

__all__ = ["AVATAR", "FakeRoom", "FakeSimli"]

SESSION_TOKEN = "simli-session-token-xyz"


class FakeSimli:
    def __init__(self, room: FakeRoom) -> None:
        self.room = room
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.token_status = 200
        self.join_status = 200
        self.token_payload: dict[str, Any] = {"session_token": SESSION_TOKEN}
        self.join_payload: dict[str, Any] = {"status": "ok", "session_id": "simli-1"}
        self.raise_on: dict[str, BaseException] = {}

    async def __call__(self, url, headers, body, timeout):
        stage = "token" if url.endswith("/compose/token") else "join"
        self.calls.append((url, dict(headers), dict(body)))
        if stage in self.raise_on:
            raise self.raise_on[stage]
        if stage == "token":
            return self.token_status, dict(self.token_payload)
        if self.join_status < 300:
            self.room.avatar_joins()
        return self.join_status, dict(self.join_payload)

    def stage(self, name: str) -> list[tuple[str, dict[str, str], dict[str, Any]]]:
        suffix = "/compose/token" if name == "token" else "/integrations/livekit/agents"
        return [c for c in self.calls if c[0].endswith(suffix)]
