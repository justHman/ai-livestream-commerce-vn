"""P2-08: container-build must build ONLY the affected images.

The review finding: `.github/workflows/ci.yml` `container-build` guards on
`services_json != '[]'` but its matrix is a static 4-service `include` — a
backend-only change still builds llm/tts/avatar images. The OpenSpec spec
(`ci-container-build-optimization`) says container-build SHALL run for the
affected images only.

Two GitHub matrix mechanisms were tried and rejected:
1. `include`-as-lookup against a `fromJson(services_json)` vector — GitHub
   ALWAYS adds include entries that do not match a vector row as brand-new
   rows, so non-affected services still build.
2. A compute job emitting the exact matrix via `fromJson(outputs.matrix)` —
   evals empty for reusable-workflow jobs, so `container-build` fails with an
   empty-matrix error instead of spawning.

The reliable pattern: a STATIC 4-row config matrix plus a job-level `if` that
uses the matrix context (`contains(fromJson(services_json), matrix.service)`).
GitHub evaluates the job `if` per matrix row, so non-affected rows are skipped
(not built); the gate accepts `skipped`. Each row carries the full service
area id (`backend_service` — the same id `_python-service-ci.yml` consumes as
`services/product/<service>` paths) plus a short `scope` for the stable
per-service Buildx cache key.
"""

from __future__ import annotations

import importlib.util as _util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"

_gha_yaml = _util.spec_from_file_location(
    "_gha_yaml",
    ROOT / "scripts" / "ci" / "_gha_yaml.py",
)
_mod = _util.module_from_spec(_gha_yaml)
_gha_yaml.loader.exec_module(_mod)
load_yaml = _mod.load_file

# Service area ids emitted by detect_changes.py (docstring: "backend_service,
# llm_service, tts_service, avatar_service") — PRODUCT_AREAS = {s}_service.
SERVICE_FULL_NAMES = ("backend_service", "llm_service", "tts_service", "avatar_service")
SHORT_SCOPES = ("backend", "llm", "tts", "avatar")


def _container_build_job() -> dict[str, Any]:
    doc = load_yaml(CI_YML)
    assert doc is not None, "ci.yml must parse"
    job = doc["jobs"]["container-build"]
    assert job.get("uses") == "./.github/workflows/_container-build.yml"
    return job


def test_container_build_job_if_filters_by_services_json() -> None:
    """container-build must guard each matrix row on affected-area output.

    A backend-only change must not schedule llm/tts/avatar image builds; the
    job-level `if` reads services_json and the matrix context so non-affected
    rows are skipped.
    """
    job = _container_build_job()
    if_cond = job.get("if", "")
    assert "services_json" in if_cond, "container-build if must read services_json"
    assert "matrix.service" in if_cond, "container-build if must use the matrix context"
    assert "contains(fromJson" in if_cond, (
        "container-build if must filter rows via contains(fromJson(services_json), matrix.service)"
    )


def test_container_build_if_skips_when_no_services() -> None:
    """Docs-only (services_json == '[]') -> the whole job is skipped."""
    job = _container_build_job()
    assert "services_json != '[]'" in job.get("if", ""), (
        "docs-only must skip the entire container-build job (empty services_json)"
    )


def test_container_build_matrix_covers_all_four_services() -> None:
    """The static config matrix must cover every canonical product service."""
    matrix = _container_build_job()["strategy"]["matrix"]
    include = matrix.get("include", [])
    assert [entry["service"] for entry in include] == list(SERVICE_FULL_NAMES)
    for entry in include:
        # Full service area id (matches services_json / service-ci path).
        assert entry["service"] in SERVICE_FULL_NAMES
        # Short cache scope stays stable per service (never area id/branch/SHA).
        assert entry["scope"] in SHORT_SCOPES
        assert entry["dockerfile"].startswith("services/product/")
        assert entry["image"].startswith("imjusthman/ai-live-")
        assert entry["platforms"]


def test_container_build_rows_have_all_build_inputs() -> None:
    """Each matrix row carries everything _container-build.yml needs."""
    matrix = _container_build_job()["strategy"]["matrix"]
    for entry in matrix["include"]:
        for key in ("service", "scope", "dockerfile", "image", "platforms"):
            assert entry.get(key), f"matrix row {entry['service']} missing {key!r}"
