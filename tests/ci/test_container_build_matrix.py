"""P2-08: container-build must build ONLY the affected images.

The review finding: `.github/workflows/ci.yml` `container-build` guards on
`services_json != '[]'` but its matrix is a static 4-service `include` — a
backend-only change still builds llm/tts/avatar images. The OpenSpec spec
(`ci-container-build-optimization`) says container-build SHALL run for the
affected images only.

GitHub matrix filtering attempts that were tried and rejected:
1. `include`-as-lookup against a `fromJson(services_json)` vector — GitHub
   ALWAYS adds include entries that do not match a vector row as new rows.
2. A compute job emitting the exact matrix via `fromJson(outputs.matrix)` —
   evals empty for reusable-workflow jobs.
3. `matrix.service` in the CALLER job `if` — the matrix context is not
   allowed in a job-level `if` that calls a reusable workflow.

The working pattern: a STATIC 4-row config matrix, each row passing
`enabled: ${{ contains(fromJson(services_json), matrix.service) }}` to the
reusable workflow, whose build job guards on `if: ${{ inputs.enabled }}`
(inputs ARE allowed in a reusable-workflow job `if`). Non-affected rows are
skipped inside the reusable workflow; the gate accepts `skipped`.
"""

from __future__ import annotations

import importlib.util as _util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
CB_YML = ROOT / ".github" / "workflows" / "_container-build.yml"

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


def test_container_build_passes_enabled_per_row() -> None:
    """Each matrix row must pass `enabled` derived from services_json.

    `contains(fromJson(services_json), matrix.service)` — GitHub evaluates
    `with` per matrix row, so non-affected services get enabled=false and are
    skipped inside the reusable workflow.
    """
    job = _container_build_job()
    enabled = job["with"].get("enabled", "")
    assert "contains(fromJson(needs.affected-area.outputs.services_json), matrix.service)" in enabled, (
        "container-build must pass enabled = (service in services_json) per matrix row"
    )


def test_container_build_job_if_skips_when_no_services() -> None:
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
        assert entry["service"] in SERVICE_FULL_NAMES
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


def test_reusable_workflow_build_job_guards_on_enabled() -> None:
    """_container-build.yml's build job must skip when enabled is false.

    This is the actual filter: inputs ARE allowed in a reusable-workflow job
    `if` (the caller's matrix context is not), so non-affected rows are
    skipped here.
    """
    doc = load_yaml(CB_YML)
    assert doc is not None, "_container-build.yml must parse"
    build_job = doc["jobs"]["build"]
    assert "inputs.enabled" in build_job.get("if", ""), (
        "reusable build job must guard on if: ${{ inputs.enabled }}"
    )


def test_reusable_workflow_declares_enabled_input() -> None:
    """The reusable workflow must declare the `enabled` input (default true)."""
    doc = load_yaml(CB_YML)
    assert doc is not None
    inputs = doc["on"]["workflow_call"]["inputs"]
    assert "enabled" in inputs
    assert inputs["enabled"].get("default") is True, "enabled must default to true"
