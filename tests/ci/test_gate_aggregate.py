"""Gate aggregation logic + static CI-gate completeness tests (R8.5)."""

import re
from pathlib import Path

from scripts.ci.gate_aggregate import aggregate, main

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


# ── Failure-injection unit tests ────────────────────────────────────────────


def test_repo_tools_failure_fails_gate():
    failures = aggregate({"repo-tools": "failure"})
    assert failures
    assert any("repo-tools" in f for f in failures)


def test_all_success_and_skipped_passes():
    failures = aggregate({"scan": "success", "repo-tools": "success", "workbench-check": "skipped"})
    assert failures == []


def test_unknown_status_fails():
    failures = aggregate({"service-ci": "cancelled"})
    assert failures
    assert any("service-ci" in f for f in failures)


def test_missing_result_value_fails_closed():
    failures = aggregate({"repo-tools": ""})
    assert failures


def test_cli_returns_nonzero_on_failure():
    assert main(["--result", "repo-tools=failure"]) == 1


def test_cli_returns_zero_on_success():
    assert main(["--result", "scan=success", "--result", "repo-tools=skipped"]) == 0


def test_cli_rejects_malformed_result():
    assert main(["--result", "bogus"]) == 2


# ── Static: the CI gate must aggregate every governed job (R8.5) ────────────


def _gate_aggregate_run_text(gate: dict) -> str:
    """The aggregation step text (not steps[0] — checkout may precede it)."""
    for step in gate["steps"]:
        if "gate_aggregate.py" in (step.get("run") or ""):
            return step["run"]
    raise AssertionError("gate job has no step invoking gate_aggregate.py")


def test_ci_gate_aggregates_every_needs_job():
    from scripts.ci._gha_yaml import load_file

    doc = load_file(WORKFLOWS / "ci.yml")
    gate = doc["jobs"]["gate"]
    needed = gate["needs"]
    assert isinstance(needed, list) and len(needed) >= 9

    run_text = _gate_aggregate_run_text(gate)
    # Every governed job must be passed to gate_aggregate as --result <name>=
    for job in needed:
        assert re.search(rf"--result\s+[A-Za-z0-9_-]*{re.escape(job)}=", run_text), (
            f"gate aggregation omits governed job '{job}'"
        )
    # The aggregation must invoke the shared script, not a shell-only loop.
    assert "gate_aggregate.py" in run_text


def test_repo_tools_explicitly_aggregated():
    from scripts.ci._gha_yaml import load_file

    doc = load_file(WORKFLOWS / "ci.yml")
    gate = doc["jobs"]["gate"]
    assert "repo-tools" in gate["needs"]
    agg = next(s for s in gate["steps"] if "gate_aggregate.py" in (s.get("run") or ""))
    env = agg.get("env") or {}
    assert "REPO" in env, "repo-tools result must be wired into the gate env"
    run_text = agg["run"]
    assert re.search(r"--result\s+repo-tools=", run_text)
