"""
Task 82 — OpenSpec 2.3: Static workflow validation.

Parses YAML safely with a pinned/declared tooling dependency. Enforces:
- Event entry names/triggers and underscore reusable workflow_call only
- Unsupported trigger events are rejected (not silently accepted)
- Reusable local refs exist and use allowed ref form
- No reusable workflow has push/PR/dispatch/schedule/repository_dispatch trigger
- Service tags exact `<service>-vSEMVER` pattern (error, not warning)
- No implicit deployment on CI (job-level `uses`, step-level `uses` and `run`)
- Validate permissions/environment/secrets reference shape without reading values
- Add fail fixtures for each rule and run against repo
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

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

# Canonical service short names allowed in `<short>-vSEMVER` release tags.
SERVICE_TAG_NAMES = frozenset({"backend", "llm", "tts", "avatar"})

# Service tag pattern: `<service>-vSEMVER`, exact canonical list.
SERVICE_TAG_PATTERN = re.compile(r"^([a-z][a-z0-9_]*)-v\d+\.\d+\.\d+$")

# Forbidden on CI
CI_FORBIDDEN_TRIGGERS = frozenset({"workflow_dispatch"})

# Keywords that indicate a deploy/apply mutation in a run or step.
DEPLOY_KEYWORDS = (
    "deploy",
    "ecs update-service",
    "kubectl apply",
    "terraform apply",
    "aws ecs",
)

# Known deploy-capable action references (uses form) treated as a deploy mutation.
DEPLOY_ACTIONS = (
    "aws-actions/amazon-ecs-deploy-task-definition@",
    "azure/webapps-deploy@",
    "google-github-actions/deploy-cloudrun@",
    "google-github-actions/deploy-appengine@",
    "cloudflare/wrangler-action@",
    "SamKirkland/FTP-Deploy-Action@",
    "selefra/terraform-apply-action@",
    "hashicorp/terraform-github-actions@",
)

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


def _on_mapping(on: object) -> Dict[str, object]:
    """Normalize the `on` key into a {event: filter} mapping."""
    if on is None:
        return {}
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {e: {} for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return {k: v for k, v in on.items()}
    return {}


def validate_trigger_rules(workflow: dict, result: ValidationResult) -> None:
    """Validate trigger/event rules.

    Rules:
    - R1: Event entry workflows use descriptive names without leading underscore
    - R2: Reusable workflows MUST use workflow_call only
    - R3: Reusable workflows MUST NOT have push/PR/dispatch/schedule/repository_dispatch
    - R4: CI workflow MUST NOT have workflow_dispatch trigger
    - R5: Unsupported trigger events are rejected (no silent pass)
    """
    on = _on_mapping(workflow.get("on"))

    triggers: Set[str] = set(on.keys())

    # R5: Reject any trigger not in the supported set.
    unsupported = triggers - SUPPORTED_TRIGGERS
    if unsupported:
        result.add_error(
            f"Unsupported trigger(s): {', '.join(sorted(unsupported))}. "
            f"Supported triggers: {', '.join(sorted(SUPPORTED_TRIGGERS))}."
        )

    is_reusable = "workflow_call" in triggers
    is_entry = not is_reusable
    filename = Path(result.workflow_path).name

    # R1+R2+R3 combined: reusable workflows MUST be underscore-prefixed and
    # expose workflow_call ONLY.
    if is_reusable:
        extra_triggers = triggers - {"workflow_call"}
        if extra_triggers:
            result.add_error(
                f"Reusable workflow '{filename}' has non-workflow_call triggers: "
                f"{', '.join(sorted(extra_triggers))}. "
                f"Reusable workflows must use workflow_call only."
            )
        if not filename.startswith("_"):
            result.add_error(
                f"Reusable workflow '{filename}' must use a leading underscore "
                f"filename (reusable = workflow_call only)."
            )

    # R3: Reusable: no entry-only triggers
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
    if is_entry and filename.startswith("_"):
        result.add_error(
            f"Entry workflow '{filename}' starts with underscore. "
            f"Entry workflows must use descriptive names without leading underscore."
        )


def validate_reusable_refs(workflow: dict, result: ValidationResult, workflows_dir: Path) -> None:
    """Validate reusable workflow references.

    Rules:
    - R6: Reusable local refs must exist as .yml files
    - R7: Reusable refs must use allowed form: ./github/workflows/<name>.yml
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
        target = workflows_dir / Path(uses).name
        if not target.exists():
            result.add_error(
                f"Job '{job_name}' references reusable workflow '{uses}' "
                f"but the file does not exist at '{target}'."
            )


def validate_service_tags(workflow: dict, result: ValidationResult) -> None:
    """Validate service tag patterns.

    Rules:
    - R8: Service tags must match `<service>-vSEMVER` exactly; a tag that does
          not match or uses an unsupported service is an ERROR.
    """
    on = _on_mapping(workflow.get("on"))

    push = on.get("push")
    if not isinstance(push, dict):
        return

    tags = push.get("tags")
    if not isinstance(tags, list):
        return

    for tag_pattern in tags:
        if not isinstance(tag_pattern, str):
            continue
        # Glob-style broad tags (e.g. `v*`) are allowed at the entry level but
        # a pattern that looks like a service tag must be exact.
        m = SERVICE_TAG_PATTERN.match(tag_pattern)
        if m:
            service_short = m.group(1)
            if service_short not in SERVICE_TAG_NAMES:
                result.add_error(
                    f"Service tag pattern '{tag_pattern}' uses unsupported service "
                    f"'{service_short}'. Allowed: {', '.join(sorted(SERVICE_TAG_NAMES))}."
                )
        # Service-shaped tag that fails SEMVER or is v-prefixed-but-not-service:
        # reject as malformed release tag. Covers `backend-v1.2`, `v1.2.3`,
        # `backend-v`, and any `<name>-v...` shape.
        elif _is_service_shaped(tag_pattern) or (
            tag_pattern != "v*" and tag_pattern.startswith("v")
        ):
            result.add_error(
                f"Service tag pattern '{tag_pattern}' is not a valid release tag. "
                f"Must match <service>-vMAJOR.MINOR.PATCH, e.g. backend-v1.2.0."
            )


def _is_service_shaped(tag: str) -> bool:
    """True if a tag looks like `<word>-v...` (service release shape)."""
    return bool(re.match(r"^[a-z][a-z0-9_]*-[vV]", tag))


def _is_deploy_uses(uses: str) -> bool:
    """True if a uses reference is a known deploy-capable action."""
    return any(uses.startswith(prefix) for prefix in DEPLOY_ACTIONS)


def validate_no_deploy(workflow: dict, result: ValidationResult) -> None:
    """Validate CI workflow has no implicit deployment.

    Rules:
    - R9: CI must NOT contain a deployment step/action. Checks job-level `uses`
          and step-level `uses` + `run` for deploy keywords.
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

        # Job-level reusable deploy action
        job_uses = job.get("uses")
        if isinstance(job_uses, str) and _is_deploy_uses(job_uses):
            result.add_error(
                f"CI workflow job '{job_name}' uses deploy action '{job_uses}'. CI must not deploy."
            )

        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue

        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and _is_deploy_uses(uses):
                result.add_error(
                    f"CI workflow job '{job_name}' step {idx} uses deploy action "
                    f"'{uses}'. CI must not deploy."
                )
            run = step.get("run", "")
            if isinstance(run, str) and any(keyword in run.lower() for keyword in DEPLOY_KEYWORDS):
                result.add_error(
                    f"CI workflow job '{job_name}' step {idx} has deployment command "
                    f"'{run[:80]}...'. CI must not deploy."
                )


def validate_permissions_shape(workflow: dict, result: ValidationResult) -> None:
    """Validate permissions block and environment/secret reference shape.

    Rules:
    - R10: Deployment workflows MUST have a permissions block that is a mapping.
    - R11: Job `environment` reference must be a string or a mapping with a
           `name` (never an unexpected type).
    - R12: `secrets:` block references must be a mapping (shape only, values
           never read).
    """
    filename = Path(result.workflow_path).name
    is_deploy = "deploy" in filename

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    # R10: deploy workflows need a permissions block
    if is_deploy:
        perms = workflow.get("permissions")
        if perms is None:
            result.add_error(
                f"Deployment workflow '{filename}' must declare a 'permissions' block."
            )
        elif not isinstance(perms, (dict, str)):
            result.add_error(
                f"Deployment workflow '{filename}' permissions block must be a "
                f"mapping or 'read-all'/'write-all' string; got {type(perms).__name__}."
            )

    # R11/R12: per-job environment + secrets shape
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        env = job.get("environment")
        if env is not None and not isinstance(env, (str, dict)):
            result.add_error(
                f"Job '{job_name}' has an environment reference that is not a "
                f"string or mapping: {type(env).__name__}."
            )
        secrets = job.get("secrets")
        if secrets is not None and not isinstance(secrets, dict):
            result.add_error(
                f"Job '{job_name}' secrets block must be a mapping "
                f"(shape check only); got {type(secrets).__name__}."
            )

    # Workflow-level secrets (reusable): must be a mapping
    wf_secrets = workflow.get("secrets")
    if wf_secrets is not None and not isinstance(wf_secrets, dict):
        result.add_error(
            f"Workflow '{filename}' secrets block must be a mapping "
            f"(shape check only); got {type(wf_secrets).__name__}."
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
    validate_no_deploy(workflow, result)
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
