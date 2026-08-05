"""Explicit semantic embedder readiness contracts."""

from __future__ import annotations

import builtins

import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig
from backend.application.director.embeddings import HashingEmbedder, build_embedder, embedder_status
from backend.application.director.session_context import DirectorRuntime


def test_hash_mode_is_explicit_and_degraded() -> None:
    status = embedder_status(build_embedder(mode="hash-explicit"))

    assert status == {
        "name": "hashing-fallback",
        "mode": "hash-explicit",
        "ready": True,
        "degraded": True,
        "error": None,
    }


def test_semantic_required_fails_loud_when_dependency_is_missing(monkeypatch) -> None:
    real_import = builtins.__import__

    def fail_sentence_transformers(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ModuleNotFoundError("sentence_transformers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sentence_transformers)

    with pytest.raises(RuntimeError, match="semantic embedder unavailable"):
        build_embedder(mode="semantic-required")


def test_health_ready_reports_director_embedder_status(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    backend = MockRenderBackend()
    runtime = DirectorRuntime(backend, embedder=HashingEmbedder())
    dependencies = _Deps(
        backend=backend,
        
        
        director=runtime,
        
        config=AppConfig(app_env="dev", render_backend="mock", director_enabled=True),
    )
    from backend.main import create_app

    app = create_app(deps=dependencies)
    with TestClient(app) as client:
        body = client.get("/api/v1/health/ready").json()

    assert body["embedder"]["name"] == "hashing-fallback"
    assert body["embedder"]["degraded"] is True
    assert body["ok"] is True

from avatar.engines.mock import MockRenderBackend
from conftest import make_deps as _Deps  # noqa: F401
