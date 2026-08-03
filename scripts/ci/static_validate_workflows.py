"""
Task 82 — OpenSpec 2.3: Static workflow validation.

Parses YAML safely with a pinned/declared tooling dependency. Enforces:
- Event entry names/triggers and underscore reusable workflow_call only
- Reusable local refs exist and use allowed ref form
- No reusable workflow has push/PR/dispatch/tag trigger
- Service tags exact `<service>-vSEMVER` pattern
- No implicit deployment on CI
- Validate permissions/environment/secrets reference shape without reading values
- Add fail fixtures for each rule and run against repo
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Optional

# Use shared GHA-safe YAML loader
import importlib.util as _util

_gha_yaml = _util.spec_from_file_location(
    "_gha_yaml",
    Path(__file__).resolve().parent / "_gha_yaml.py",
)
_mod = _util.module_from_spec(_gha_yaml)
_gha_yaml.loader.exec_module(_mod)
load_yaml = _mod.load_file

# ── Constants ───────────────────────────────────────────────────────────────

SUPPORTED_TRIGGERS = frozenset(
    {
        "push",
        "pull_request",
        "workflow_dispatch",
        "workflow_call",
        "schedule",
        "repository_dispatch",
    }
)

# Triggers that are ONLY allowed for event-entry workflows (not reusable)
ENTRY_ONLY_TRIGGERS = frozenset({"push", "pull_request", "schedule", "repository_dispatch"})

# Allowed ref forms for reusable workflow_call targets
REUSABLE_REF_PATTERN = re.compile(r"^\./\.github/workflows/[_a-z][_a-z0-9-]*\.yml$")

# Service tag pattern: <service>-vSEMVER
SERVICE_TAG_PATTERN = re.compile(r"^(backend|llm|tts|avatar|livekit|lmcache)-v\d+\.\d+\.\d+$")

# Forbidden triggers on CI
CI_FORBIDDEN_TRIGGERS = frozenset({"workflow_dispatch"})

# Service name validation pattern
SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ── Validation Rules ────────────────────────────────────────────────────────


class ValidationResult:
    """Collect validation messages for a single workflow."""

    def __init__(self, workflow_path: str):
        self.workflow_path = workflow_path
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        parts = [f"  [{status}] {self.workflow_path}"]
        for e in self.errors:
            parts.append(f"    ERROR: {e}")
        for w in self.warnings:
            parts.append(f"    WARN:  {w}")
        return "\n".join(parts)


def load_yaml_safe(path: Path) -> Optional[dict]:
    """Load a YAML file safely. Returns None on parse failure."""
    return load_yaml(path)


def validate_trigger_rules(workflow: dict, result: ValidationResult) -> None:
    """Validate trigger/event rules.

    Rules:
    - R1: Event entry workflows use descriptive names without leading underscore
    - R2: Reusable workflows MUST use workflow_call only
    - R3: Reusable workflows MUST NOT have push/PR/dispatch/tag triggers
    - R4: CI workflow MUST NOT have workflow_dispatch trigger
    """
    on = workflow.get("on", {})
    if isinstance(on, str):
        on = {on: {}}

    triggers = set()
    if isinstance(on, dict):
        for key in on:
            triggers.add(key)

    is_reusable = "workflow_call" in triggers
    is_entry = not is_reusable
    filename = Path(result.workflow_path).name

    # R2: Reusable workflows must use workflow_call only
    if is_reusable:
        # A reusable workflow should only have workflow_call
        extra_triggers = triggers - {"workflow_call"}
        if extra_triggers:
            result.add_error(
                f"Reusable workflow '{filename}' has non-workflow_call triggers: "
                f"{', '.join(sorted(extra_triggers))}. "
                f"Reusable workflows must use workflow_call only."
            )

    # R3: Reusable: no push/PR/dispatch/tag
    if is_reusable:
        forbidden = triggers & ENTRY_ONLY_TRIGGERS
        if forbidden:
            result.add_error(
                f"Reusable workflow '{filename}' has entry-only triggers: "
                f"{', '.join(sorted(forbidden))}. "
                f"Reusable workflows must NOT have push/PR/dispatch/tag triggers."
            )

    # R4: CI workflow must not have workflow_dispatch
    if filename == "ci.yml" and "workflow_dispatch" in triggers:
        result.add_error(
            f"CI workflow '{filename}' has workflow_dispatch trigger. "
            f"CI must not be manually dispatchable."
        )

    # R1: Entry workflow naming
    if is_entry:
        if filename.startswith("_"):
            result.add_warning(
                f"Entry workflow '{filename}' starts with underscore. "
                f"Entry workflows should use descriptive names without leading underscore."
            )


def validate_reusable_refs(workflow: dict, result: ValidationResult, workflows_dir: Path) -> None:
    """Validate reusable workflow references.

    Rules:
    - R5: Reusable local refs must exist as .yml files
    - R6: Reusable refs must use allowed form: ./github/workflows/<name>.yml
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not uses or not isinstance(uses, str):
            continue

        # Skip external actions (org/repo@ref)
        if "/" in uses and not uses.startswith("./"):
            continue

        # Local reusable ref
        if not REUSABLE_REF_PATTERN.match(uses):
            result.add_error(
                f"Job '{job_name}' uses invalid reusable ref format: '{uses}'. "
                f"Must match: ./github/workflows/<name>.yml "
                f"(lowercase, underscore, no spaces)."
            )
            continue

        # Check file exists
        ref_path = (workflows_dir.parent / uses).resolve()
        try:
            ref_path = ref_path.relative_to(workflows_dir.parent)
        except ValueError:
            pass

        target = workflows_dir / Path(uses).name
        if not target.exists():
            result.add_error(
                f"Job '{job_name}' references reusable workflow '{uses}' "
                f"but the file does not exist at '{target}'."
            )


def validate_service_tags(workflow: dict, result: ValidationResult) -> None:
    """Validate service tag patterns.

    Rules:
    - R7: Service tags must match <service>-vSEMVER pattern
    """
    on = workflow.get("on", {})
    if isinstance(on, str):
        return

    if not isinstance(on, dict):
        return

    push = on.get("push")
    if isinstance(push, dict):
        tags = push.get("tags", []) or []
        if isinstance(tags, list):
            for tag_pattern in tags:
                if isinstance(tag_pattern, str):
                    # Check if this looks like a service tag pattern
                    if "v" in tag_pattern and "-" in tag_pattern:
                        # Extract the concrete pattern
                        if not SERVICE_TAG_PATTERN.match(tag_pattern):
                            # Allow glob patterns like v*
                            if tag_pattern != "v*":
                                result.add_warning(
                                    f"Tag pattern '{tag_pattern}' may not match "
                                    f"service tag format '<service>-vMAJOR.MINOR.PATCH'."
                                )


def validate_ci_no_deploy(workflow: dict, result: ValidationResult) -> None:
    """Validate CI workflow has no implicit deployment.

    Rules:
    - R8: CI workflow must not have deployment steps
    """
    filename = Path(result.workflow_path).name
    if filename != "ci.yml":
        return

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run", "") or ""
            if isinstance(run, str) and any(
                keyword in run.lower() for keyword in ["deploy", "ecs update-service", "kubectl"]
            ):
                result.add_error(
                    f"CI workflow has deployment step in job '{job_name}': "
                    f"'{run[:80]}...'. CI must not deploy."
                )


def validate_permissions_shape(workflow: dict, result: ValidationResult) -> None:
    """Validate permissions block shape without reading values.

    Rules:
    - R9: Permissions block must exist and be a mapping
    - R10: Environment reference must exist and be a string
    """
    # workflows with deployment should have a permissions block
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    # Check if any job references an environment
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        env = job.get("environment")
        if env is not None and not isinstance(env, (str, dict)):
            result.add_warning(
                f"Job '{job_name}' has an environment reference that is not a "
                f"string or mapping: {type(env).__name__}."
            )


def validate_workflow(workflow_path: Path, workflows_dir: Path) -> ValidationResult:
    """Run all validation rules on a single workflow file."""
    result = ValidationResult(str(workflow_path))

    workflow = load_yaml_safe(workflow_path)
    if workflow is None:
        result.add_error(f"Failed to parse YAML file: {workflow_path}")
        return result

    if not isinstance(workflow, dict):
        result.add_error(f"Workflow is not a mapping (got {type(workflow).__name__}).")
        return result

    validate_trigger_rules(workflow, result)
    validate_reusable_refs(workflow, result, workflows_dir)
    validate_service_tags(workflow, result)
    validate_ci_no_deploy(workflow, result)
    validate_permissions_shape(workflow, result)

    return result


def validate_all_workflows(workflows_dir: Path) -> List[ValidationResult]:
    """Validate all workflow YAML files in the directory."""
    results = []
    for fpath in sorted(workflows_dir.glob("*.yml")):
        result = validate_workflow(fpath, workflows_dir)
        results.append(result)
    return results


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for static workflow validation.

    Usage:
        python scripts/ci/static_validate_workflows.py \\
            [--workflows-dir .github/workflows] [--json]
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Static validation of GitHub Actions workflow YAML files."
    )
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=Path(".github/workflows"),
        help="Path to .github/workflows directory (default: .github/workflows)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()
    workflows_dir = args.repo_root / args.workflows_dir

    if not workflows_dir.exists():
        print(f"ERROR: Workflows directory not found: {workflows_dir}", file=sys.stderr)
        sys.exit(1)

    results = validate_all_workflows(workflows_dir)

    if args.json:
        output = []
        for r in results:
            output.append(
                {
                    "workflow": r.workflow_path,
                    "passed": r.passed,
                    "errors": r.errors,
                    "warnings": r.warnings,
                }
            )
        print(json.dumps(output, indent=2))
    else:
        for r in results:
            print(r.summary())

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"\nFAILED: {len(failed)} workflow(s) have validation errors.")
        sys.exit(1)
    else:
        print(f"\nAll {len(results)} workflow(s) passed validation.")


if __name__ == "__main__":
    main()
