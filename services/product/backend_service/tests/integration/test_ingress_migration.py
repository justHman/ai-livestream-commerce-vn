"""Ingress migration gate (C1). Fails while any removed viewer-ingress route
is still mounted; passes only after C2 removes all three.

Removed routes:
- /api/v1/ws/platform/{session_id}           (legacy viewer comment ingest)
- /api/v1/sessions/{session_id}/ingest       (legacy viewer comment ingest)
- /api/v1/sessions/{session_id}/chat         (legacy viewer comment ingest)

Canonical replacement: POST /api/v1/sessions/{session_id}/events.
"""

from __future__ import annotations

import pytest

from backend.config import AppConfig
from conftest import make_deps as _Deps  # noqa: F401


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


def _mounted_paths(cfg: AppConfig) -> set[str]:
    from backend.main import create_app

    deps = _Deps(config=cfg)
    app = create_app(config=cfg, deps=deps)
    paths: set[str] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        # Newer Starlette wraps included routers in _IncludedRouter; older
        # versions expose APIRouter sub-routes directly via .routes.
        original = getattr(route, "original_router", None)
        sub_routes = getattr(route, "routes", None)
        if original is not None:
            stack.extend(original.routes)
        elif sub_routes is not None:
            stack.extend(sub_routes)
        elif getattr(route, "path", ""):
            paths.add(route.path)
    return paths


def test_platform_ws_route_removed(mock_env: None) -> None:
    cfg = AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    assert "/api/v1/ws/platform/{session_id}" not in _mounted_paths(cfg)


def test_sessions_ingest_route_removed(mock_env: None) -> None:
    cfg = AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    assert "/api/v1/sessions/{session_id}/ingest" not in _mounted_paths(cfg)


def test_sessions_chat_route_removed(mock_env: None) -> None:
    cfg = AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    assert "/api/v1/sessions/{session_id}/chat" not in _mounted_paths(cfg)


def test_sessions_events_route_mounted(mock_env: None) -> None:
    cfg = AppConfig(render_backend="mock", app_env="dev", debug_enabled=False)
    assert "/api/v1/sessions/{session_id}/events" in _mounted_paths(cfg)
