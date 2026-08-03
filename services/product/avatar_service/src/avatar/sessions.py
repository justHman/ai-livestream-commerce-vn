"""Avatar session lifecycle owner.

`SessionManager` coordinates the engine and the LiveKit publisher:
create, interrupt, stop, and cleanup with bounded lifecycle. Authorized
start returns only browser-safe LiveKit URL + client token; API/provider
credentials stay server-side (Task 1.30).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from avatar.engines.base import AvatarEngine, StartOptions, StartResult
from avatar.publishing.livekit import LiveKitPublisher


@dataclass
class _SessionRecord:
    session_id: str
    status: str = "active"
    extra: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Coordinates engine lifecycle and LiveKit publication per session."""

    def __init__(
        self,
        engine: AvatarEngine,
        publisher: LiveKitPublisher,
    ) -> None:
        self._engine = engine
        self._publisher = publisher
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = threading.Lock()

    @property
    def engine(self) -> AvatarEngine:
        return self._engine

    @property
    def publisher(self) -> LiveKitPublisher:
        return self._publisher

    def create(self, opts: StartOptions) -> StartResult:
        """Start an engine session, mint a browser-safe LiveKit token, return
        only browser-safe connection data."""
        result = self._engine.start(opts)
        room = result.session_id
        identity = f"avatar-{result.session_id}"
        token = self._publisher.client_token(room=room, identity=identity)
        with self._lock:
            self._sessions[result.session_id] = _SessionRecord(
                session_id=result.session_id,
                status="active",
                extra={"room": room, "identity": identity},
            )
        return StartResult(
            session_id=result.session_id,
            livekit_url=self._publisher.livekit_url,
            livekit_client_token=token,
            mode=result.mode,
        )

    def interrupt(self, session_id: str) -> None:
        self._engine.interrupt(session_id)
        with self._lock:
            record = self._sessions.get(session_id)
            if record is not None:
                record.status = "interrupted"

    def stop(self, session_id: str) -> None:
        self._engine.stop(session_id)
        with self._lock:
            self._sessions.pop(session_id, None)

    def status(self, session_id: str) -> str:
        with self._lock:
            record = self._sessions.get(session_id)
        if record is None:
            return self._engine.session_status(session_id)
        return record.status

    def cleanup(self) -> None:
        """Stop all active sessions during bounded shutdown."""
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            try:
                self.stop(session_id)
            except Exception:
                pass
        try:
            self._engine.unload()
        finally:
            self._publisher.close()