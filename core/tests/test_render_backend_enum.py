"""Renderer selector contract tests."""

from __future__ import annotations

import pytest

from core.config import AppConfig
from core.render.cloud import CloudRenderBackend
from core.render.mock import MockRenderBackend
from core.render.self_host import SelfHostRenderBackend


def _backend(monkeypatch: pytest.MonkeyPatch, selector: str):
    monkeypatch.setenv("RENDER_BACKEND", selector)
    monkeypatch.setenv("AVATAR_BASE_URL", "http://avatar:8080")
    monkeypatch.setenv("LIVEAVATAR_API_KEY", "test-key")
    return AppConfig.from_env().build_render_backend()


def test_cloud_liveavatar_selects_cloud_backend(monkeypatch: pytest.MonkeyPatch):
    assert isinstance(_backend(monkeypatch, "cloud_liveavatar"), CloudRenderBackend)


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
    assert isinstance(backend, SelfHostRenderBackend)
    assert backend.model == model


def test_mock_selects_mock_backend(monkeypatch: pytest.MonkeyPatch):
    assert isinstance(_backend(monkeypatch, "mock"), MockRenderBackend)


def test_remote_avatar_is_not_public_selector(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="remote_avatar"):
        _backend(monkeypatch, "remote_avatar")


def test_unknown_renderer_fails_closed(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="unknown RENDER_BACKEND"):
        _backend(monkeypatch, "not-a-renderer")
