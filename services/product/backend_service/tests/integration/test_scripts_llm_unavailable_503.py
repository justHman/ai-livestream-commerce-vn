"""Router test: domain error ``llm_unavailable`` maps to HTTP 503.

Change B5: the production ScriptAuthoringService (parallel cluster B4) raises
``ScriptAuthoringError("llm_unavailable", ...)`` from its AI methods when the
LLM is unavailable. Per the repo error-handling rule, engine unavailability
must surface as 503. This test locks that mapping at the router boundary for
all four AI commands.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.application.script_authoring.service import ScriptAuthoringError

from conftest import make_deps as _Deps  # noqa: F401


class FakeLlmUnavailableService:
    """Minimal in-memory fake: every AI method raises ``llm_unavailable``."""

    async def start_generation(
        self,
        *,
        set_id: str,
        product_id: str,
        target_duration_s: int,
        intent: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise ScriptAuthoringError("llm_unavailable", "llm not available")

    async def fix_with_ai(
        self,
        *,
        set_id: str,
        product_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise ScriptAuthoringError("llm_unavailable", "llm not available")

    async def regenerate_segment(
        self,
        *,
        set_id: str,
        product_id: str,
        segment_index: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise ScriptAuthoringError("llm_unavailable", "llm not available")

    async def start_batch_generation(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        target_duration_s: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise ScriptAuthoringError("llm_unavailable", "llm not available")


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


@pytest.fixture
def client(mock_env: None) -> TestClient:
    """App with the llm-unavailable fake injected into the container."""
    from backend.application.render.mock import MockRenderBackend
    from backend.config import AppConfig

    deps = _Deps(
        backend=MockRenderBackend(),
        config=AppConfig(render_backend="mock", app_env="dev"),
    )
    deps.script_authoring_service = FakeLlmUnavailableService()

    from backend.main import create_app

    app = create_app(config=deps.config, deps=deps)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "/api/v1/script-sets/set-1/products/P001/generate",
            {"target_duration_s": 600, "intent": "selling"},
        ),
        (
            "POST",
            "/api/v1/script-sets/set-1/products/P001/fix",
            {},
        ),
        (
            "POST",
            "/api/v1/script-sets/set-1/products/P001/segments/0/regenerate",
            {},
        ),
        (
            "POST",
            "/api/v1/script-sets/set-1/generate-batch",
            {"product_ids": ["P001"], "target_duration_s": 600},
        ),
    ],
)
def test_ai_endpoint_llm_unavailable_maps_to_503(
    client: TestClient, method: str, path: str, body: dict[str, Any]
) -> None:
    resp = client.request(method, path, json=body)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "llm_unavailable"
