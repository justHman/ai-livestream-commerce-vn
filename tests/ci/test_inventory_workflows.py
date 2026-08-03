"""Tests for OpenSpec 2.1 workflow inventory script."""

import json
import re
from pathlib import Path

import pytest

from scripts.ci.inventory_workflows import (
    inventory_all,
    inventory_workflow,
    validate_inventory,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

ACTION_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
LOCAL_REF = re.compile(r"^\./\.github/workflows/[a-z0-9_-]+\.yml$")


def _all_workflows() -> list[str]:
    return [p.name for p in sorted(WORKFLOWS.glob("*.yml"))]


def _inv(name: str) -> dict:
    return inventory_workflow(WORKFLOWS / name, WORKFLOWS)


# ── Coverage ────────────────────────────────────────────────────────────────


def test_all_workflows_inventoried():
    inventory = inventory_all(WORKFLOWS)
    inventoried = {Path(i["file"]).name for i in inventory}
    assert inventoried == set(_all_workflows())


def test_every_workflow_classified():
    for wf in inventory_all(WORKFLOWS):
        assert wf["role"] in {"event-entry", "reusable"}, wf["file"]


def test_every_workflow_has_canonical_target():
    for name in _all_workflows():
        wf = _inv(name)
        assert wf.get("canonical_target"), f"{name}: missing canonical target"


def test_every_workflow_has_mutation_classification():
    for name in _all_workflows():
        wf = _inv(name)
        m = wf["mutation"]
        assert set(m.keys()) == {"artifact_push", "deploy", "infra_mutation"}


# ── Triggers ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,event",
    [
        ("ci.yml", "push"),
        ("ci.yml", "pull_request"),
        ("deploy-dev.yml", "push"),
        ("deploy-prod.yml", "push"),
        ("deploy-prod.yml", "workflow_dispatch"),
        ("build-images.yml", "workflow_dispatch"),
        ("seed-weights.yml", "workflow_dispatch"),
    ],
)
def test_expected_triggers_present(name, event):
    events = [t["event"] for t in _inv(name)["triggers"]]
    assert event in events


def test_ci_has_no_manual_deploy_trigger():
    events = [t["event"] for t in _inv("ci.yml")["triggers"]]
    assert "workflow_dispatch" not in events


# ── Jobs / steps / secrets / environments ───────────────────────────────────


def test_every_workflow_has_jobs():
    for name in _all_workflows():
        wf = _inv(name)
        assert wf["jobs"], f"{name} has no jobs"


def test_job_environment_captured():
    # deploy-dev preflight + deploy jobs use environment: development
    wf = _inv("deploy-dev.yml")
    envs = {j.get("environment") for j in wf["jobs"]}
    assert "development" in envs


def test_job_secrets_shape_captured():
    # jobs may not reference secrets (deploy-dev uses env + AWS role); the
    # schema must expose the secrets field as a list regardless.
    wf = _inv("deploy-dev.yml")
    for job in wf["jobs"]:
        assert isinstance(job.get("secrets"), list)


def test_step_uses_captured():
    wf = _inv("ci.yml")
    uses = {u for job in wf["jobs"] for u in job.get("uses", [])}
    assert "actions/checkout@v4" in uses
    assert "astral-sh/setup-uv@v5" in uses


def test_mutation_classification_correct():
    assert _inv("deploy-dev.yml")["mutation"]["deploy"] is True
    assert _inv("build-images.yml")["mutation"]["artifact_push"] is True
    assert _inv("build-images.yml")["mutation"]["deploy"] is False
    assert _inv("ci.yml")["mutation"]["deploy"] is False


def test_service_tags_captured():
    assert _inv("deploy-prod.yml")["service_tags"] == ["v*"]


def test_path_filters_captured():
    pf = _inv("deploy-dev.yml").get("path_filters")
    assert pf is not None
    assert "services/**" in pf


def test_referenced_actions_have_valid_form():
    """Every uses: reference must be a valid action or an existing local reusable."""
    for name in _all_workflows():
        wf = _inv(name)
        for job in wf["jobs"]:
            for u in job.get("uses", []):
                if u.startswith("./"):
                    assert LOCAL_REF.match(u), f"{name}: bad local ref {u}"
                    target = WORKFLOWS / Path(u).name
                    assert target.exists(), f"{name}: missing local ref {u}"
                else:
                    assert ACTION_REF.match(u), f"{name}: bad action ref {u}"


# ── Structural validation ───────────────────────────────────────────────────


def test_structural_validation_passes_on_repo():
    errors = validate_inventory(inventory_all(WORKFLOWS), WORKFLOWS)
    assert errors == []


def test_structural_validation_detects_missing_ref(tmp_path):
    wf_dir = tmp_path
    (wf_dir / "bad.yml").write_text(
        "name: bad\non:\n  workflow_call:\njobs:\n  a:\n    uses: ./.github/workflows/missing.yml\n"
    )
    inv = [inventory_workflow(wf_dir / "bad.yml", wf_dir)]
    errors = validate_inventory(inv, wf_dir)
    assert any("missing reusable ref" in e for e in errors)


# ── Manifest / JSON ─────────────────────────────────────────────────────────


def test_json_manifest_roundtrip(tmp_path):
    manifest = tmp_path / "inventory.json"
    import subprocess

    proc = subprocess.run(
        [
            "python",
            "scripts/ci/inventory_workflows.py",
            "--repo-root",
            ".",
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(manifest.read_text())
    assert len(payload["workflows"]) == len(_all_workflows())


def test_manifest_drift_detected(tmp_path):
    manifest = tmp_path / "drift.json"
    manifest.write_text('{"workflows": []}', encoding="utf-8")
    import subprocess

    proc = subprocess.run(
        [
            "python",
            "scripts/ci/inventory_workflows.py",
            "--repo-root",
            ".",
            "--check-drift",
            str(manifest),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "drift" in proc.stderr
