"""Canonical service identity + migration candidate-binding tests (R1.8/R1.10)."""

from pathlib import Path

from scripts.ci._gha_yaml import load_file
from scripts.ci.service_identity import (
    by_service_dir,
    container,
    identity,
    service_ids,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


# ── Canonical table ─────────────────────────────────────────────────────────


def test_canonical_service_ids():
    assert service_ids() == frozenset({"backend", "llm", "tts", "avatar"})


def test_canonical_rows_have_expected_fields():
    assert identity("backend")["service_dir"] == "backend_service"
    assert identity("backend")["image"] == "imjusthman/ai-live-backend"
    assert identity("backend")["container"] == "backend"
    assert identity("backend")["dockerfile"] == "services/product/backend_service/Dockerfile"
    assert identity("llm")["service_dir"] == "llm_service"
    assert identity("tts")["service_dir"] == "tts_service"
    assert identity("avatar")["service_dir"] == "avatar_service"


def test_by_service_dir_reverse_map():
    m = by_service_dir()
    assert m["backend_service"] == "backend"
    assert m["llm_service"] == "llm"
    assert m["tts_service"] == "tts"
    assert m["avatar_service"] == "avatar"


# ── Parity with other validators ────────────────────────────────────────────


def test_parity_with_static_validator_tag_names():
    from scripts.ci.static_validate_workflows import SERVICE_TAG_NAMES

    assert SERVICE_TAG_NAMES == service_ids()


def test_parity_with_workflow_input_short_ids():
    from scripts.ci.validate_workflow_inputs import SERVICE_SHORT

    assert frozenset(SERVICE_SHORT.values()) == service_ids()


# ── Workflow matrix drift ───────────────────────────────────────────────────


def _matrix_rows(doc, job_name):
    job = (doc.get("jobs") or {}).get(job_name) or {}
    include = (job.get("strategy") or {}).get("matrix") or {}
    return include.get("include", [])


def test_ci_container_build_matrix_matches_table():
    doc = load_file(WORKFLOWS / "ci.yml")
    rows = _matrix_rows(doc, "container-build")
    assert len(rows) == 4
    m = by_service_dir()
    for row in rows:
        assert row["service"] in m, f"unknown service_dir {row['service']}"
        sid = m[row["service"]]
        exp = identity(sid)
        assert row["image"] == exp["image"], f"{sid} image mismatch"
        assert row["dockerfile"] == exp["dockerfile"], f"{sid} dockerfile mismatch"
        assert row["platforms"] == exp["platforms"], f"{sid} platforms mismatch"


def test_deploy_dev_matrices_match_table():
    doc = load_file(WORKFLOWS / "deploy-dev.yml")
    m = by_service_dir()
    for job in ("build", "deploy"):
        rows = _matrix_rows(doc, job)
        assert len(rows) == 4
        for row in rows:
            assert row["service"] in m
            sid = m[row["service"]]
            exp = identity(sid)
            assert row["image"] == exp["image"], f"{sid} {job} image mismatch"
            if "dockerfile" in row:
                assert row["dockerfile"] == exp["dockerfile"]
                assert row["platforms"] == exp["platforms"]
            if "container" in row:
                assert row["container"] == exp["container"]


def test_deploy_staging_matrices_match_table():
    doc = load_file(WORKFLOWS / "deploy-staging.yml")
    m = by_service_dir()
    for job in ("build", "deploy"):
        rows = _matrix_rows(doc, job)
        assert len(rows) == 4
        for row in rows:
            assert row["service"] in m
            sid = m[row["service"]]
            exp = identity(sid)
            assert row["image"] == exp["image"]
            if "dockerfile" in row:
                assert row["dockerfile"] == exp["dockerfile"]
                assert row["platforms"] == exp["platforms"]
            if "container" in row:
                assert row["container"] == exp["container"]


def test_container_short_id_matches_table():
    """release-service uses service_short as the container name for promotion."""
    assert container("backend") == "backend"
    assert container("llm") == "llm"
    assert container("tts") == "tts"
    assert container("avatar") == "avatar"


# ── Migration binds the exact candidate backend image (R1.10) ───────────────


def _migrate_run_text(workflow_name):
    doc = load_file(WORKFLOWS / workflow_name)
    job = (doc.get("jobs") or {}).get("migrate") or {}
    for st in job.get("steps", []):
        if isinstance(st, dict) and st.get("name") == "Run pre-deploy migration":
            return st.get("run") or ""
    raise AssertionError(f"{workflow_name}: migrate job has no 'Run pre-deploy migration' step")


def test_dev_migration_binds_candidate_digest():
    run = _migrate_run_text("deploy-dev.yml")
    assert "containerOverrides" in run, "migration must override the candidate container image"
    assert '"image"' in run, "migration must bind the exact candidate image"
    assert "--overrides" in run, "migration must pass an ECS overrides document"
    assert "backend" in run, "override must target the canonical backend container"


def test_staging_migration_binds_candidate_digest():
    run = _migrate_run_text("deploy-staging.yml")
    assert "containerOverrides" in run
    assert '"image"' in run
    assert "--overrides" in run


def test_release_migration_binds_candidate_digest():
    run = _migrate_run_text("release-service.yml")
    assert "containerOverrides" in run
    assert '"image"' in run
    assert "--overrides" in run
