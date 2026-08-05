"""Render backend stop_all() delegates every active session to stop().

The canonical avatar_service owns the render backends. The cloud
(``CloudRenderBackend``) and in-process ``RemoteAvatarBackend`` classes were
removed in the split — cloud rendering runs in the avatar_service media plane
and is reached through the backend's HTTP client seam. This test verifies the
surviving canonical backends snapshot and delegate ``stop_all()``.

Migrated from ``core/tests/test_render_stop_all.py`` (OpenSpec 1.50).
"""

from __future__ import annotations

from avatar.engines.base import RenderBackend, StartOptions
from avatar.engines.mock import MockRenderBackend


class _RecordingMock(MockRenderBackend):
    def __init__(self) -> None:
        super().__init__()
        self.stopped: list[str] = []

    def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)
        super().stop(session_id)


def test_mock_stop_all_snapshots_and_delegates_to_stop():
    backend = _RecordingMock()
    first = backend.start(StartOptions())
    second = backend.start(StartOptions())

    backend.stop_all()

    assert set(backend.stopped) == {first.session_id, second.session_id}
    assert backend._sessions == {}


def test_base_stop_all_is_safe_noop_for_stateless_backends():
    class _Stateless(RenderBackend):
        name = "stateless"

        def start(self, opts: StartOptions):
            raise NotImplementedError

        def interrupt(self, session_id: str) -> None:
            return None

        def stop(self, session_id: str) -> None:
            return None

    # Must not raise for a backend that tracks nothing.
    _Stateless().stop_all()
