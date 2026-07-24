"""Render backend stop_all() delegates every active session to stop()."""

from __future__ import annotations

from core.render.cloud import CloudRenderBackend
from core.render.remote_avatar import RemoteAvatarBackend


class _RecordingCloud(CloudRenderBackend):
    def __init__(self) -> None:
        self._convos = {"one": object(), "two": object()}
        self.stopped: list[str] = []

    def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)
        self._convos.pop(session_id)


class _RecordingRemote(RemoteAvatarBackend):
    def __init__(self) -> None:
        self._sessions = {"one", "two"}
        self.stopped: list[str] = []

    def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)
        self._sessions.remove(session_id)


def test_cloud_stop_all_snapshots_and_delegates_to_stop():
    backend = _RecordingCloud()

    backend.stop_all()

    assert backend.stopped == ["one", "two"]
    assert backend._convos == {}


def test_remote_stop_all_snapshots_and_delegates_to_stop():
    backend = _RecordingRemote()

    backend.stop_all()

    assert set(backend.stopped) == {"one", "two"}
    assert backend._sessions == set()
