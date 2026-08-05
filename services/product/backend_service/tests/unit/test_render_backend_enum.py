"""Renderer selector contract tests (canonical backend, OpenSpec 1.50).

The canonical backend ``AppConfig.build_render_backend()`` builds the offline
mock renderer locally; cloud/self-host selections return a thin remote
placeholder because the real renderers live in the avatar_service (media
plane) and are reached over HTTP. ``remote_avatar`` is not a public selector.

Migrated from ``core/tests/test_render_backend_enum.py``.
"""

from __future__ import annotations

import pytest

from avatar.engines.mock import MockRenderBackend
from backend.config import AppConfig


def _backend(monkeypatch: pytest.MonkeyPatch, selector: str):
    monkeypatch.setenv("RENDER_BACKEND", selector)
    monkeypatch.setenv("AVATAR_BASE_URL", "http://avatar:8080")
    monkeypatch.setenv("LIVEAVATAR_API_KEY", "test-key")
    return AppConfig.from_env().build_render_backend()


def test_cloud_liveavatar_selects_remote_placeholder(monkeypatch: pytest.MonkeyPatch):
    backend = _backend(monkeypatch, "cloud_liveavatar")
    assert backend.name == "cloud_liveavatar"
    # The control plane must not embed LiveAvatar SDK code; start() fails loud.
    from backend.application.render.engines_base import StartOptions

    with pytest.raises(RuntimeError, match="avatar_service"):
        backend.start(StartOptions())


@pytest.mark.parametrize(
    ("selector", "model"),
    [
        ("self_host_avatarforcing_half", "avatarforcing"),
        ("self_host_echoavatar_full", "echoavatar"),
    ],
)
def test_self_host_selectors_keep_selected_model(
    monkeypatch: pytest.MonkeyPatch, selector: str, model: str
):
    backend = _backend(monkeypatch, selector)
    assert backend.name == selector


def test_mock_selects_mock_backend(monkeypatch: pytest.MonkeyPatch):
    assert isinstance(_backend(monkeypatch, "mock"), MockRenderBackend)


def test_remote_avatar_is_not_public_selector(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="remote_avatar"):
        _backend(monkeypatch, "remote_avatar")


def test_unknown_renderer_fails_closed(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="unknown RENDER_BACKEND"):
        _backend(monkeypatch, "not-a-renderer")
