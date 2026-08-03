"""Tests for OpenSpec 2.1 workflow inventory script."""

import re
from pathlib import Path

import pytest

from scripts.ci.inventory_workflows import inventory_all, inventory_workflow


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

ACTION_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
LOCAL_REF = re.compile(r"^\./\.github/workflows/[a-z0-9_-]+\.yml$")


def _all_workflows() -> list[str]:
    return [p.name for p in sorted(WORKFLOWS.glob("*.yml"))]


def test_all_workflows_inventoried():
    inventory = inventory_all(WORKFLOWS)
    inventoried = {Path(i["file"]).name for i in inventory}
    assert inventoried == set(_all_workflows())


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
    wf = inventory_workflow(WORKFLOWS / name)
    events = [t["event"] for t in wf["triggers"]]
    assert event in events


def test_ci_has_no_manual_deploy_trigger():
    wf = inventory_workflow(WORKFLOWS / "ci.yml")
    events = [t["event"] for t in wf["triggers"]]
    assert "workflow_dispatch" not in events


def test_every_workflow_has_jobs():
    for name in _all_workflows():
        wf = inventory_workflow(WORKFLOWS / name)
        assert wf["jobs"], f"{name} has no jobs"


def test_referenced_actions_have_valid_form():
    """Every uses: reference must be a valid action or an existing local reusable."""
    for name in _all_workflows():
        wf = inventory_workflow(WORKFLOWS / name)
        for job in wf["jobs"]:
            for u in job.get("uses", []):
                if u.startswith("./"):
                    assert LOCAL_REF.match(u), f"{name}: bad local ref {u}"
                    target = WORKFLOWS / Path(u).name
                    assert target.exists(), f"{name}: missing local ref {u}"
                else:
                    assert ACTION_REF.match(u), f"{name}: bad action ref {u}"
