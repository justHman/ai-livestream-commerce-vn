"""Workbench structure contracts for the Stage 2 operator console (Task 1.43/1.44).

The legacy one-file stage2.html was decomposed into the canonical Vite
workbench. Behavior-equivalent coverage now lives in:
  - workbench/__tests__/*.ts (Vitest module behavior)
  - workbench/playwright/*.spec.ts (browser E2E)

These tests assert the canonical layout that replaces the old static page:
one index.html, flat responsibility modules, fixture/token sources, no legacy
entrypoint references, and no /lite/* usage from the workbench API surface.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = ROOT / "workbench"
SRC = WORKBENCH / "src"
INDEX = (WORKBENCH / "index.html").read_text(encoding="utf-8")

REQUIRED_MODULES = (
    "main.ts",
    "api.ts",
    "api_types.ts",
    "websocket.ts",
    "state.ts",
    "sessions.ts",
    "resources.ts",
    "diagnostics.ts",
    "livekit.ts",
    "simulator.ts",
    "styles.css",
    "fixtures.ts",
    "dev_tokens.ts",
)


def _module_names() -> set[str]:
    return {name for name in REQUIRED_MODULES if (SRC / name).is_file()}


def test_workbench_has_single_index_html() -> None:
    html_files = [p for p in WORKBENCH.iterdir() if p.suffix == ".html"]
    assert html_files == [WORKBENCH / "index.html"]


def test_all_flat_responsibility_modules_exist() -> None:
    assert _module_names() == set(REQUIRED_MODULES)


def test_index_boots_main_module_once() -> None:
    assert INDEX.count('type="module" src="/src/main.ts"') == 1
    assert "stage2.html" not in INDEX
    assert "lite.html" not in INDEX


def test_index_exposes_canonical_console_panels() -> None:
    block_ids = (
        "sessionPanel",
        "resourcePanel",
        "shopPanel",
        "productsPanel",
        "videoPanel",
        "autoDemoPanel",
        "diagnosticsPanel",
        "eventLogPanel",
    )
    assert all(INDEX.count(f'id="{panel}"') == 1 for panel in block_ids)


def test_index_has_runtime_config_and_validation_controls() -> None:
    controls = (
        "qaMaxClusters",
        "qaTimeout",
        "qaCooldown",
        "answerVariants",
        "preparedDepth",
        "retryCount",
        "pivotEnter",
        "pivotExit",
        "applyRuntimeConfigBtn",
        "productsJson",
        "configErrors",
    )
    assert all(f'id="{control}"' in INDEX for control in controls)


def test_index_preserves_accessibility_contracts() -> None:
    styles = (SRC / "styles.css").read_text(encoding="utf-8")
    assert ":focus-visible" in styles
    assert '@media (max-width: 1024px)' in styles
    assert 'aria-live="polite"' in INDEX
    assert "sr-only" in styles


def test_workbench_src_never_uses_legacy_aliases() -> None:
    for module in REQUIRED_MODULES:
        path = SRC / module
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "/lite/" not in text, f"{module} references /lite/*"
        assert "debug/mock_viewer_msgs" not in text, f"{module} uses debug API"
        assert "debug/clusters" not in text, f"{module} uses debug clusters"
        assert "mock/video" not in text, f"{module} uses MJPEG debug media"
        assert "mock/frame" not in text, f"{module} uses debug frames"


def test_workbench_api_points_to_canonical_sessions_paths() -> None:
    api_source = (SRC / "api.ts").read_text(encoding="utf-8")
    ws_source = (SRC / "websocket.ts").read_text(encoding="utf-8")
    assert "/api/v1/sessions" in api_source
    assert "ws/control" in ws_source and "ws/platform" in ws_source
    assert "media/livekit/room" in api_source


def test_fixture_json_exists_with_expected_sets() -> None:
    fixtures = WORKBENCH / "src" / "fixtures"
    for name in ("shop_profiles.json", "viewer_messages.json", "products.json"):
        assert (fixtures / name).is_file()
    products = json.loads((fixtures / "products.json").read_text(encoding="utf-8"))
    assert len(products) >= 3
    assert all(p["id"] and p["name"] for p in products)


def test_dev_tokens_exact_literals_live_only_in_dev_tokens_source() -> None:
    dev_tokens = (SRC / "dev_tokens.ts").read_text(encoding="utf-8")
    assert "local-test-token-123456789012345678901234567890" in dev_tokens
    assert "local-admin-token-123456789012345678901234567890" in dev_tokens
    # No other workbench source should embed those exact literals.
    for spec in REQUIRED_MODULES:
        if spec == "dev_tokens.ts":
            continue
        path = SRC / spec
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "local-test-token-123456789012345678901234567890" not in text
            assert "local-admin-token-123456789012345678901234567890" not in text


def test_no_superseded_static_console_entrypoints() -> None:
    frontend = ROOT / "frontend"
    if frontend.exists():
        html_files = [p for p in frontend.iterdir() if p.suffix == ".html"]
        # The old console was a single blob; after migration no console entrypoint
        # may remain in frontend/. index.html/lite.html may be retired separately,
        # but stage2.html must be gone (decomposition complete).
        names = {p.name for p in html_files}
        assert "stage2.html" not in names