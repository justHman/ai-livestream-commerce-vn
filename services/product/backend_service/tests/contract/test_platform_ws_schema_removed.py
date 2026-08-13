"""Static guard: platform WebSocket schema must be removed after migration.

The multi-platform-agentic-live-director change removes the platform
WebSocket contract. Until that lands, this file FAILS on purpose: the
generated artifact still exists on disk and the repo-root generator
(scripts/contracts/) still emits it. After the removal cluster lands, the
removal assertions flip to pass while the retained /ws/control contract
assertion keeps guarding the survivor.

Static, text-based checks only: scripts/contracts/generate.py imports the
FastAPI app at module import, so we never import it — we read the source.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
GENERATE_PY = REPO_ROOT / "scripts" / "contracts" / "generate.py"
CHECK_PY = REPO_ROOT / "scripts" / "contracts" / "check.py"
WS_DIR = REPO_ROOT / "services" / "product" / "backend_service" / "contracts" / "v1" / "websocket"


def test_platform_ws_schema_artifact_is_removed() -> None:
    assert not (WS_DIR / "platform.schema.json").exists()


def test_platform_ws_schema_not_generated_by_generate_py() -> None:
    assert "platform.schema.json" not in GENERATE_PY.read_text()


def test_platform_ws_schema_not_listed_in_check_py() -> None:
    assert "platform.schema.json" not in CHECK_PY.read_text()


def test_control_ws_schema_artifact_is_kept() -> None:
    assert (WS_DIR / "control.schema.json").exists()
