"""Tests for OpenSpec 2.1 workflow inventory script."""

import json
import re
from pathlib import Path

import pytest

from scripts.ci.inventory_workflows import (
    inventory_all,
    inventory_workflow,
    step_push_semantics,
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
        ("deploy-dev.yml", "workflow_dispatch"),
        ("deploy-staging.yml", "workflow_dispatch"),
        ("release-service.yml", "push"),
        ("build-images.yml", "workflow_dispatch"),
        ("seed-weights.yml", "workflow_dispatch"),
    ],
)
def test_expected_triggers_present(name, event):
    events = [t["event"] for t in _inv(name)["triggers"]]
    assert event in events


def test_deploy_prod_triggers_disabled():
    """6.4: deploy-prod.yml is superseded by release-service.yml and has no
    live triggers (push tags v* and workflow_dispatch both removed)."""
    wf = _inv("deploy-prod.yml")
    events = [t["event"] for t in wf["triggers"]]
    assert events == []


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
    # setup-uv moved into the reusable _python-service-ci.yml (3.x rewrite); the
    # entry workflow references the reusable instead of inlining the action.
    reusable = _inv("_python-service-ci.yml")
    reusable_uses = {u for job in reusable["jobs"] for u in job.get("uses", [])}
    assert "astral-sh/setup-uv@v5" in uses or "astral-sh/setup-uv@v5" in reusable_uses


def test_mutation_classification_correct():
    assert _inv("deploy-dev.yml")["mutation"]["deploy"] is True
    assert _inv("build-images.yml")["mutation"]["artifact_push"] is True
    assert _inv("build-images.yml")["mutation"]["deploy"] is False
    assert _inv("ci.yml")["mutation"]["deploy"] is False


def test_service_tags_captured():
    # deploy-prod triggers are disabled (6.4); release-service owns tag releases.
    assert _inv("deploy-prod.yml")["service_tags"] == []


def test_dispatch_only_has_no_path_filters():
    # deploy-dev is dispatch-only (OpenSpec 4.1): the push trigger and path
    # filters are removed; deployment is driven by explicit commit/service
    # inputs instead of a push event.
    wf = _inv("deploy-dev.yml")
    assert wf.get("path_filters") in (None, [])
    events = [t["event"] for t in wf["triggers"]]
    assert events == ["workflow_dispatch"]


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


# ── Finding 1: secret refs, push:false, canonical manifest ─────────────────


def test_push_false_string_not_artifact_mutation():
    """step_push_semantics('false') returns False (string truthiness fix)."""
    assert (
        step_push_semantics({"uses": "docker/build-push-action@v6", "with": {"push": "false"}})
        is False
    )


def test_push_true_string_semantics():
    """step_push_semantics('true') returns True."""
    assert (
        step_push_semantics({"uses": "docker/build-push-action@v6", "with": {"push": "true"}})
        is True
    )


def test_push_yaml_bool_true_semantics():
    """step_push_semantics(True) returns True."""
    assert (
        step_push_semantics({"uses": "docker/build-push-action@v6", "with": {"push": True}}) is True
    )


def test_push_yaml_bool_false_semantics():
    """step_push_semantics(False) returns False."""
    assert (
        step_push_semantics({"uses": "docker/build-push-action@v6", "with": {"push": False}})
        is False
    )


def test_push_missing_defaults_true():
    """step_push_semantics with no push key defaults to True (action default)."""
    assert step_push_semantics({"uses": "docker/build-push-action@v6", "with": {}}) is True


def test_non_build_push_action_returns_false():
    """step_push_semantics for non-build-push action returns False."""
    assert step_push_semantics({"uses": "actions/checkout@v4", "with": {"push": "true"}}) is False


def test_secret_refs_captured_from_step_with():
    """Secret references in step with: block are captured without values."""
    wf = inventory_workflow(WORKFLOWS / "deploy-dev.yml", WORKFLOWS)
    all_secrets = set()
    for job in wf["jobs"]:
        all_secrets.update(job.get("secrets", []))
    # deploy-dev uses AWS_ROLE_ARN_DEV in configure-aws-credentials steps.
    assert "AWS_ROLE_ARN_DEV" in all_secrets
    # Docker Hub credentials moved into the reusable deploy/build workflows
    # (4.1): they must be inventoried there, not duplicated in the caller.
    reusable = inventory_workflow(WORKFLOWS / "_deploy-service.yml", WORKFLOWS)
    reusable_secrets = set()
    for job in reusable["jobs"]:
        reusable_secrets.update(job.get("secrets", []))
    assert "DOCKERHUB_USER" in reusable_secrets
    assert "DOCKERHUB_TOKEN" in reusable_secrets


def test_secret_refs_captured_from_step_env():
    """Secret references in step env: block are captured."""
    wf = inventory_workflow(WORKFLOWS / "deploy-prod.yml", WORKFLOWS)
    all_secrets = set()
    for job in wf["jobs"]:
        all_secrets.update(job.get("secrets", []))
    assert "DOCKERHUB_USER" in all_secrets


def test_push_false_not_artifact_mutation():
    """ci.yml uses docker/build-push-action with push: false — not artifact_push."""
    wf = inventory_workflow(WORKFLOWS / "ci.yml", WORKFLOWS)
    assert wf["mutation"]["artifact_push"] is False, (
        "push:false step should not count as artifact_push"
    )


def test_canonical_manifest_write_drift_consistent(tmp_path):
    """Manifest write and drift check use identical JSON serialization."""
    import subprocess

    manifest = tmp_path / "canonical.json"
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
    # Re-run with --check-drift against the same manifest must pass
    proc2 = subprocess.run(
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
    assert proc2.returncode == 0, proc2.stderr
