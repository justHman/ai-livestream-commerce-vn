"""Contract: committed TTS OpenAPI matches the built app and excludes health."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tts import create_app

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "v1" / "openapi.json"


def test_contract_file_exists() -> None:
    assert CONTRACT_PATH.exists()


def test_contract_excludes_health() -> None:
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert not any(p.startswith("/health") for p in spec["paths"])


def test_contract_matches_built_app() -> None:
    app = create_app()
    spec = app.openapi()
    for path in [p for p in list(spec.get("paths", {})) if p.startswith("/health")]:
        del spec["paths"][path]
    committed = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert json.dumps(spec, sort_keys=True) == json.dumps(committed, sort_keys=True)


def test_contract_paths_present() -> None:
    spec = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert "/v1/speech" in spec["paths"]
    assert "/v1/voices" in spec["paths"]


def test_engine_unavailable_returns_503() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine = None
        resp = client.get("/v1/voices")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "engine_unavailable"
