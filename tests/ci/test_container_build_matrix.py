"""P2-08: container-build matrix must be derived from affected services.

The review finding: `.github/workflows/ci.yml` `container-build` guards on
`services_json != '[]'` but its matrix is a static 4-service `include` — a
backend-only change still builds llm/tts/avatar images. The OpenSpec spec
(`ci-container-build-optimization`) says container-build SHALL run for the
affected images only.

The derived matrix uses GitHub's `include`-as-lookup semantics: a `service`
vector (`fromJson(needs.affected-area.outputs.services_json)`) joined against
4 include entries keyed on the same short service names. Docs-only changes
emit `[]` -> zero rows; the `if:` guard already skips the job.
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

# Short service names: the affected-area step emits `backend`, `llm`, `tts`,
# `avatar` (proven by _python-service-ci.yml consuming the same services_json
# as matrix.service for `services/product/${{ matrix.service }}` paths).
SERVICE_SHORT_NAMES = ("backend", "llm", "tts", "avatar")


def _container_build_job() -> dict[str, Any]:
    doc = load_yaml(CI_YML)
    assert doc is not None, "ci.yml must parse"
    job = doc["jobs"]["container-build"]
    assert job.get("uses") == "./.github/workflows/_container-build.yml"
    return job


def _expand(matrix: dict[str, Any], services: list[str]) -> list[dict[str, Any]]:
    """Model GitHub matrix expansion: vector rows enriched by include lookup."""
    include = matrix.get("include", [])
    if "service" not in matrix:
        return include
    rows = []
    for service in services:
        for entry in include:
            if entry.get("service") == service:
                rows.append(entry)
                break
    return rows


# ── RED: matrix must be derived from affected-area services_json ─────────────
def test_container_build_matrix_derived_from_services_json() -> None:
    """The container-build matrix must consume the trusted affected output.

    Without the `service` vector, the static include always expands to all 4
    images regardless of which services changed — the bug this test encodes.
    """
    matrix = _container_build_job()["strategy"]["matrix"]
    assert "service" in matrix, (
        "container-build matrix must be derived from needs.affected-area.outputs.services_json"
    )
    assert "fromJson(needs.affected-area.outputs.services_json)" in matrix["service"], (
        "matrix.service must read the affected-area services_json"
    )


def test_container_build_backend_only_change_builds_only_backend() -> None:
    """A backend-only change must produce exactly one image job: backend."""
    matrix = _container_build_job()["strategy"]["matrix"]
    rows = _expand(matrix, ["backend"])
    assert [row["service"] for row in rows] == ["backend"]
    assert all(row["image"] == "imjusthman/ai-live-backend" for row in rows)


def test_container_build_docs_only_change_builds_zero_images() -> None:
    """Docs-only (services_json == '[]') -> zero image jobs."""
    matrix = _container_build_job()["strategy"]["matrix"]
    rows = _expand(matrix, [])
    assert rows == []


def test_container_build_shared_lock_change_builds_zero_images() -> None:
    """Shared lockfile (uv.lock) -> shared-locks, no product service affected.

    Per the tool's real classification (``detect_affected_areas`` maps
    ``uv.lock`` to ``shared-locks`` — never a product service), services_json
    is ``[]`` and no image jobs run; the ``if:`` guard keeps the job skipped
    and the gate accepts the neutral result.
    """
    matrix = _container_build_job()["strategy"]["matrix"]
    rows = _expand(matrix, [])
    assert rows == []


def test_container_build_two_service_change_fans_out_exactly() -> None:
    """backend + tts changed -> exactly those two image jobs, no others."""
    matrix = _container_build_job()["strategy"]["matrix"]
    rows = _expand(matrix, ["backend", "tts"])
    assert [row["service"] for row in rows] == ["backend", "tts"]


def test_container_build_include_covers_all_four_services() -> None:
    """The include lookup table must cover every canonical product service."""
    matrix = _container_build_job()["strategy"]["matrix"]
    include = matrix.get("include", [])
    assert [entry["service"] for entry in include] == ["backend", "llm", "tts", "avatar"]
    assert all(entry["service"] in SERVICE_SHORT_NAMES for entry in include), (
        "include entries must key on short service names from services_json"
    )
