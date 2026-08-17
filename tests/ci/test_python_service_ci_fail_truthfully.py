"""NEW-CI-01: pytest lanes in _python-service-ci.yml must fail truthfully.

Regression contract for the reusable per-service Python CI workflow:
- The Unit / Integration / Contract / Coverage pytest steps must NOT swallow
  failures via `|| true` — a red pytest must fail the step (and the job).
- The Unit step must pass `--ignore=tests/integration` and
  `--ignore=tests/contract` as pytest arguments (before any shell `||`/`&&`),
  not as dead arguments trailing the `true` command.
- The Coverage step must select a coverage source (`--cov=<pkg>`) and enforce
  the 80% gate (`--cov-fail-under=80`) as pytest arguments, while staying
  behind the `inputs.coverage` guard.
- Only the Typecheck step is advisory (continue-on-error); pytest steps are not.
"""

import shlex
from pathlib import Path

import pytest

from scripts.ci._gha_yaml import load_file

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "_python-service-ci.yml"

PYTEST_STEP_NAMES = ("Unit tests", "Integration tests", "Contract tests", "Coverage gate")


def _load_workflow() -> dict:
    doc = load_file(WORKFLOW)
    assert doc is not None, f"could not parse {WORKFLOW}"
    return doc


def _steps() -> list[dict]:
    steps = _load_workflow()["jobs"]["checks"].get("steps", [])
    assert isinstance(steps, list), "checks job must have a steps list"
    return steps


def _step(name: str) -> dict:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in {WORKFLOW.name}")


def _pytest_argv(run: str) -> list[str]:
    """Tokenize a run command, cutting at the first shell control operator.

    Anything after `||` / `&&` is shell-level and must not be mistaken for a
    pytest argument — the cut mirrors what the shell actually executes.
    """
    cut = len(run)
    for sep in ("||", "&&"):
        idx = run.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    return shlex.split(run[:cut])


# ── No `|| true` swallows ───────────────────────────────────────────────────


@pytest.mark.parametrize("step_name", PYTEST_STEP_NAMES)
def test_pytest_step_does_not_swallow_failures(step_name):
    run = _step(step_name).get("run", "")
    assert "|| true" not in run, f"{step_name} swallows pytest failures via `|| true`"


# ── Unit step: --ignore args are real pytest arguments ─────────────────────


def test_unit_ignore_args_are_pytest_arguments():
    argv = _pytest_argv(_step("Unit tests")["run"])
    assert "--ignore=tests/integration" in argv, (
        "--ignore=tests/integration must be a pytest arg (before any || / &&)"
    )
    assert "--ignore=tests/contract" in argv, (
        "--ignore=tests/contract must be a pytest arg (before any || / &&)"
    )


# ── Coverage step: a real gate, still behind the inputs.coverage guard ─────


def test_coverage_step_stays_behind_inputs_guard():
    assert _step("Coverage gate").get("if") == "${{ inputs.coverage }}", (
        "Coverage gate must remain gated by inputs.coverage"
    )


def test_coverage_step_has_real_cov_gate():
    argv = _pytest_argv(_step("Coverage gate")["run"])
    assert any(t.startswith("--cov=") or t == "--cov" for t in argv), (
        "Coverage step must select a coverage source (--cov=<pkg>)"
    )
    assert "--cov-fail-under=80" in argv, (
        "--cov-fail-under=80 must be a pytest arg enforcing the gate"
    )


# ── Advisory is the exception, not the pytest lanes ────────────────────────


def test_pytest_steps_are_not_advisory():
    for step in _steps():
        if step.get("name") in PYTEST_STEP_NAMES:
            assert step.get("continue-on-error") is not True, (
                f"{step.get('name')} must fail CI on pytest failure, not continue-on-error"
            )


def test_typecheck_is_the_explicit_advisory_check():
    assert _step("Typecheck (non-blocking)").get("continue-on-error") is True
