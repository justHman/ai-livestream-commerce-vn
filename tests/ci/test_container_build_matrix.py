"""P2-08: container-build matrix must contain ONLY the affected images.

The review finding: `.github/workflows/ci.yml` `container-build` guards on
`services_json != '[]'` but its matrix is a static 4-service `include` — a
backend-only change still builds llm/tts/avatar images. The OpenSpec spec
(`ci-container-build-optimization`) says container-build SHALL run for the
affected images only.

GitHub's matrix `include` ALWAYS adds entries that do not match a vector row
as brand-new rows, so include-as-lookup cannot filter to affected services.
The correct pattern is a dedicated compute job (`container-build-matrix`)
that runs `scripts/ci/build_matrix.py` over the trusted `services_json` and
hands the exact matrix to `container-build` via
`strategy.matrix: ${{ fromJson(needs.container-build-matrix.outputs.matrix) }}`.
"""

from __future__ import annotations

import importlib.util as _util
import json
import sys
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
# _python-service-ci.yml consumes services_json as matrix.service for the
# `services/product/${{ matrix.service }}` path, so the vector uses the FULL
# names. Each build row carries a `scope` short name (backend, llm, tts,
# avatar) so the Buildx cache scope stays stable per service and never
# carries the area id, branch, or SHA.
SERVICE_FULL_NAMES = ("backend_service", "llm_service", "tts_service", "avatar_service")


def _load_build_matrix():
    spec = _util.spec_from_file_location(
        "build_matrix", ROOT / "scripts" / "ci" / "build_matrix.py"
    )
    module = _util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _container_build_job() -> dict[str, Any]:
    doc = load_yaml(CI_YML)
    assert doc is not None, "ci.yml must parse"
    job = doc["jobs"]["container-build"]
    assert job.get("uses") == "./.github/workflows/_container-build.yml"
    return job


# ── RED: matrix must be derived from affected-area services_json ─────────────
def test_container_build_matrix_derived_from_compute_job() -> None:
    """container-build must consume the exact matrix from the compute job."""
    matrix = _container_build_job()["strategy"]["matrix"]
    assert matrix == "${{ fromJson(needs.container-build-matrix.outputs.matrix) }}", (
        "container-build matrix must be fromJson(needs.container-build-matrix.outputs.matrix)"
    )


def test_container_build_matrix_compute_job_runs_build_matrix_script() -> None:
    """The compute job must derive rows from the trusted services_json."""
    doc = load_yaml(CI_YML)
    assert doc is not None
    job = doc["jobs"]["container-build-matrix"]
    assert job["needs"] == ["affected-area"], "compute job must depend on affected-area"
    assert "matrix" in job["outputs"]
    run = job["steps"][1]["run"]
    assert "build_matrix.py" in run, "compute job must run scripts/ci/build_matrix.py"
    assert "needs.affected-area.outputs.services_json" in run, (
        "compute job must consume services_json"
    )


def test_build_matrix_backend_only_change_builds_only_backend() -> None:
    """A backend-only change must produce exactly one image job: backend."""
    module = _load_build_matrix()
    rows = module.build_matrix(["backend_service"])
    assert [row["service"] for row in rows] == ["backend_service"]
    assert all(row["image"] == "imjusthman/ai-live-backend" for row in rows)
    assert all(row["scope"] == "backend" for row in rows), (
        "container-build cache scope must stay the short service name (stable per-service cache)"
    )
    # Row carries everything _container-build.yml needs.
    row = rows[0]
    assert row["dockerfile"] == "services/product/backend_service/Dockerfile"
    assert row["platforms"] == "linux/arm64"


def test_build_matrix_docs_only_change_builds_zero_images() -> None:
    """Docs-only (services_json == '[]') -> zero image jobs."""
    module = _load_build_matrix()
    assert module.build_matrix([]) == []


def test_build_matrix_shared_lock_change_builds_zero_images() -> None:
    """Shared lockfile (uv.lock) -> shared-locks, no product service affected.

    Per the tool's real classification (``detect_affected_areas`` maps
    ``uv.lock`` to ``shared-locks`` — never a product service), services_json
    is ``[]`` and no image jobs run; the ``if:`` guard keeps the job skipped
    and the gate accepts the neutral result.
    """
    module = _load_build_matrix()
    assert module.build_matrix([]) == []


def test_build_matrix_two_service_change_fans_out_exactly() -> None:
    """backend + tts changed -> exactly those two image jobs, no others."""
    module = _load_build_matrix()
    rows = module.build_matrix(["backend_service", "tts_service"])
    assert [row["service"] for row in rows] == ["backend_service", "tts_service"]
    assert all(row["image"] in ("imjusthman/ai-live-backend", "imjusthman/ai-live-tts") for row in rows)


def test_build_matrix_covers_all_four_services() -> None:
    """The config map must cover every canonical product service."""
    module = _load_build_matrix()
    rows = module.build_matrix(list(SERVICE_FULL_NAMES))
    assert [row["service"] for row in rows] == list(SERVICE_FULL_NAMES)
    # Every entry keeps a short cache scope and a real dockerfile/image.
    for row in rows:
        assert row["scope"] in ("backend", "llm", "tts", "avatar")
        assert row["dockerfile"].startswith("services/product/")
        assert row["image"].startswith("imjusthman/ai-live-")


def test_build_matrix_unknown_service_skipped() -> None:
    """Unknown service ids are skipped, never cause a broken build row."""
    module = _load_build_matrix()
    rows = module.build_matrix(["backend_service", "not_a_service"])
    assert [row["service"] for row in rows] == ["backend_service"]


def test_build_matrix_cli_outputs_json() -> None:
    """The CLI prints the same matrix the job hands to container-build."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci" / "build_matrix.py"), "--services", '["backend_service"]'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [row["service"] for row in rows] == ["backend_service"]
