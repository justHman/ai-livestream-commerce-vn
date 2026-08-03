"""Tests for OpenSpec 2.3 static workflow validation."""

from pathlib import Path

import pytest

from scripts.ci.static_validate_workflows import (
    SERVICE_TAG_PATTERN,
    load_yaml_safe,
    validate_all_workflows,
    validate_trigger_rules,
    validate_reusable_refs,
    validate_ci_no_deploy,
    ValidationResult,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


# ── Fixtures ────────────────────────────────────────────────────────────────


def _result():
    return ValidationResult("fixture.yml")


@pytest.fixture()
def workflows_dir(tmp_path):
    return tmp_path / ".github" / "workflows"


# ── Loader ──────────────────────────────────────────────────────────────────


def test_loader_keeps_on_key():
    fixture = ROOT / "tests" / "ci" / "fixtures" / "_wellformed_ci.yml"
    doc = load_yaml_safe(fixture)
    assert doc is not None
    assert "on" in doc
    assert "push" in doc["on"]


# ── Trigger rules ───────────────────────────────────────────────────────────


def test_entry_workflow_allows_push_pr_dispatch():
    r = _result()
    validate_trigger_rules({"on": {"push": {"branches": ["main"]}, "pull_request": {}}}, r)
    assert r.passed


def test_reusable_workflow_requires_workflow_call_only():
    r = _result()
    validate_trigger_rules({"on": {"workflow_call": {}, "push": {}}}, r)
    assert not r.passed
    assert any("workflow_call only" in e for e in r.errors)


def test_reusable_workflow_no_entry_triggers():
    r = _result()
    validate_trigger_rules({"on": {"workflow_call": {}, "schedule": {}}}, r)
    assert not r.passed
    assert any("entry-only triggers" in e for e in r.errors)


def test_ci_no_workflow_dispatch():
    r = ValidationResult("ci.yml")
    validate_trigger_rules({"on": {"workflow_dispatch": {}}}, r)
    assert not r.passed
    assert any("must not be manually dispatchable" in e for e in r.errors)


# ── Reusable refs ───────────────────────────────────────────────────────────


def test_invalid_local_ref_rejected(tmp_path):
    wf_dir = tmp_path
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./github/workflows/nope.yml"}}},
        r,
        wf_dir,
    )
    assert not r.passed


def test_missing_local_ref_rejected(tmp_path):
    wf_dir = tmp_path
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./.github/workflows/missing.yml"}}},
        r,
        wf_dir,
    )
    assert not r.passed
    assert any("does not exist" in e for e in r.errors)


def test_valid_local_ref_passes(tmp_path):
    (tmp_path / "_reusable.yml").write_text("name: reusable\non:\n  workflow_call:\n")
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./.github/workflows/_reusable.yml"}}},
        r,
        tmp_path,
    )
    assert r.passed


# ── Service tags ────────────────────────────────────────────────────────────


def test_service_tag_pattern():
    assert SERVICE_TAG_PATTERN.match("backend-v1.2.3")
    assert SERVICE_TAG_PATTERN.match("livekit-v0.1.0")
    assert not SERVICE_TAG_PATTERN.match("v1.2.3")
    assert not SERVICE_TAG_PATTERN.match("database-v1.2.3")
    assert not SERVICE_TAG_PATTERN.match("backend-v1.2")
    assert not SERVICE_TAG_PATTERN.match("backend-1.2.3")


# ── CI no deploy ────────────────────────────────────────────────────────────


def test_ci_no_deploy_step():
    r = ValidationResult("ci.yml")
    validate_ci_no_deploy(
        {
            "jobs": {
                "deploy": {"steps": [{"run": "aws ecs update-service --cluster x --service y"}]}
            }
        },
        r,
    )
    assert not r.passed


def test_ci_without_deploy_step_passes():
    r = ValidationResult("ci.yml")
    validate_ci_no_deploy({"jobs": {"lint": {"steps": [{"run": "uvx ruff check ."}]}}}, r)
    assert r.passed


# ── Repo-wide ───────────────────────────────────────────────────────────────


def test_all_repo_workflows_pass():
    results = validate_all_workflows(WORKFLOWS)
    for r in results:
        assert r.passed, f"{r.workflow_path}: {r.errors}"
