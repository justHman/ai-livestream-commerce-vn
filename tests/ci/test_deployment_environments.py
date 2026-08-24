"""Tests for canonical GitHub Environment vocabulary and OIDC identity (R1.3)."""

from pathlib import Path

from scripts.ci.deployment_environments import (
    GITHUB_ENVIRONMENT_NAMES,
    SUPPORTED_ENVIRONMENT_NAMES,
    expected_trust_subjects,
    trust_subject,
)
from scripts.ci.static_validate_workflows import ValidationResult
from scripts.ci.static_validate_workflows import validate_environment_vocabulary

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _result():
    return ValidationResult("fixture.yml")


def test_canonical_environment_names_are_full_words():
    assert GITHUB_ENVIRONMENT_NAMES == frozenset(
        {"development", "staging", "production"}
    )
    # The tf-side short names (dev/prod) are exactly what must NOT leak into
    # GitHub Environment references — the R1.3 mismatch.
    assert "dev" not in GITHUB_ENVIRONMENT_NAMES
    assert "prod" not in GITHUB_ENVIRONMENT_NAMES


def test_trust_subject_is_exact_environment_match():
    assert (
        trust_subject("development")
        == "repo:justHman/ai-livestream-commerce-vn:environment:development"
    )
    assert (
        trust_subject("staging")
        == "repo:justHman/ai-livestream-commerce-vn:environment:staging"
    )
    assert (
        trust_subject("production")
        == "repo:justHman/ai-livestream-commerce-vn:environment:production"
    )


def test_expected_trust_subjects_cover_all_canonical_environments():
    subs = expected_trust_subjects()
    assert set(subs) == GITHUB_ENVIRONMENT_NAMES
    for env, sub in subs.items():
        assert sub.endswith(f":environment:{env}")


def test_environment_short_name_rejected():
    r = _result()
    validate_environment_vocabulary(
        {"jobs": {"deploy": {"environment": "prod"}}}, r
    )
    assert not r.passed
    assert any("canonical" in e and "R1.3" in e for e in r.errors)


def test_environment_canonical_name_accepted():
    r = _result()
    validate_environment_vocabulary(
        {"jobs": {"deploy": {"environment": "production"}}}, r
    )
    assert r.passed


def test_environment_mapping_name_accepted():
    r = _result()
    validate_environment_vocabulary(
        {"jobs": {"deploy": {"environment": {"name": "development"}}}}, r
    )
    assert r.passed


def test_dynamic_environment_expression_skipped():
    # infra-apply uses environment: infra-${{ needs... }}; the runtime
    # allowlist owns validation, so the static rule skips expressions.
    r = _result()
    validate_environment_vocabulary(
        {"jobs": {"plan": {"environment": "infra-${{ needs.validate.outputs.validated_env }}"}}}, r
    )
    assert r.passed


def test_infra_apply_allowlist_matches_infra_env_vocabulary():
    """The infra-apply hard allowlist (dev|staging) maps onto infra-dev/infra-staging."""
    assert "infra-dev" in SUPPORTED_ENVIRONMENT_NAMES
    assert "infra-staging" in SUPPORTED_ENVIRONMENT_NAMES


def test_all_repo_workflow_environments_are_canonical():
    """Every literal workflow environment name is in the canonical vocabulary."""
    from scripts.ci._gha_yaml import load_file

    bad = []
    for f in sorted(WORKFLOWS.glob("*.yml")):
        doc = load_file(f)
        if not doc:
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            env_ref = job.get("environment")
            name = env_ref if isinstance(env_ref, str) else (env_ref or {}).get("name")
            if not isinstance(name, str) or "${{" in name:
                continue
            if name not in SUPPORTED_ENVIRONMENT_NAMES:
                bad.append(f"{f.name}: {name}")
    assert bad == [], f"non-canonical environment names: {bad}"
