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

# Canonical environment vocabulary (sibling module; load by file so the CLI
# works standalone without the repo root on sys.path).
_deploy_envs = _util.spec_from_file_location(
    "_deploy_envs",
    Path(__file__).resolve().parent / "deployment_environments.py",
)
_deploy_envs_mod = _util.module_from_spec(_deploy_envs)
_deploy_envs.loader.exec_module(_deploy_envs_mod)
SUPPORTED_ENVIRONMENT_NAMES = _deploy_envs_mod.SUPPORTED_ENVIRONMENT_NAMES

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


def validate_no_step_level_reusable(workflow: dict, result: ValidationResult) -> None:
    """Validate reusable workflows are invoked at job level only.

    Rule (audit R1.1): a reusable workflow (local `./.github/workflows/*.yml`)
    MUST be invoked via `jobs.<id>.uses`. Invoking it through `steps[*].uses`
    is invalid GitHub Actions syntax and silently breaks the delivery chain.
    External actions (`owner/repo@ref`) at step level remain allowed.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and uses.startswith("./"):
                result.add_error(
                    f"Job '{job_name}' step {idx} invokes local reusable workflow "
                    f"'{uses}' via steps[].uses. Reusable workflows must be invoked "
                    f"at job level via jobs.<id>.uses."
                )


def validate_gh_api_authentication(workflow: dict, result: ValidationResult) -> None:
    """Validate governed `gh api` calls are explicitly authenticated (audit R1.2).

    Rules:
    - Every step whose run invokes `gh api` MUST declare GH_TOKEN or
      GITHUB_TOKEN at step or job env.
    - A governed `gh api` call MUST NOT suppress CLI failure (`2>/dev/null`,
      `|| true`) and reinterpret it as absence of governance: an auth/API
      error must fail the governed check.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_env = job.get("env") if isinstance(job.get("env"), dict) else {}
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str) or "gh api" not in run:
                continue

            step_env = step.get("env") if isinstance(step.get("env"), dict) else {}
            envs = {**job_env, **step_env}
            token_keys = {k.upper() for k in envs}
            if not ({"GH_TOKEN", "GITHUB_TOKEN"} & token_keys):
                result.add_error(
                    f"Job '{job_name}' step {idx} calls `gh api` without an explicit "
                    f"GH_TOKEN/GITHUB_TOKEN env (R1.2). Declare 'env: GH_TOKEN: "
                    f"${{{{ github.token }}}}'."
                )

            # Reconstruct each logical `gh api` command (join backslash
            # continuations) and flag suppression of its exit status.
            lines = run.splitlines()
            i = 0
            while i < len(lines):
                if "gh api" not in lines[i]:
                    i += 1
                    continue
                cmd = lines[i]
                j = i
                while cmd.rstrip().endswith("\\") and j + 1 < len(lines):
                    j += 1
                    cmd += "\n" + lines[j]
                if "|| true" in cmd or "2>/dev/null" in cmd:
                    result.add_error(
                        f"Job '{job_name}' step {idx} suppresses a `gh api` failure "
                        f"(`|| true` / `2>/dev/null`). An auth/API error must fail "
                        f"the governed check, not be reinterpreted as missing evidence."
                    )
                i = j + 1


def validate_environment_vocabulary(workflow: dict, result: ValidationResult) -> None:
    """Validate job-level `environment:` names against the canonical vocabulary.

    Rule (audit R1.3): GitHub Environment names used by workflows MUST come
    from one canonical set so OIDC trust subjects match exactly. Dynamic
    expressions (containing ``${{``) are validated at runtime by the owning
    workflow's hard allowlist and are skipped here.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        env_ref = job.get("environment")
        name = None
        if isinstance(env_ref, str):
            name = env_ref
        elif isinstance(env_ref, dict):
            name = env_ref.get("name")
        if not isinstance(name, str) or not name.strip() or "${{" in name:
            continue
        if name not in SUPPORTED_ENVIRONMENT_NAMES:
            result.add_error(
                f"Job '{job_name}' uses environment '{name}' outside the canonical "
                f"vocabulary {sorted(SUPPORTED_ENVIRONMENT_NAMES)} (R1.3). OIDC "
                f"trust subjects match GitHub Environment names exactly; a "
                f"mismatch breaks OIDC and must not be worked around by "
                f"broadening trust."
            )


def validate_needs_graph(workflow: dict, result: ValidationResult) -> None:
    """Validate every `needs.X` reference points to a DIRECT declared dependency.

    Rule (audit R1.7): a job may only read another job's result/output when
    that job is declared in its `needs`. Indirect references (reading a
    transitively-depended job) are invalid and rejected statically.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        deps: Set[str] = set()
        if isinstance(needs, str):
            deps = {needs}
        elif isinstance(needs, list):
            deps = {n for n in needs if isinstance(n, str)}
        blob = json.dumps(job)
        for m in re.finditer(r"needs\.([A-Za-z_][A-Za-z0-9_-]*)\.", blob):
            dep = m.group(1)
            if dep not in deps:
                result.add_error(
                    f"Job '{job_name}' reads needs.{dep}.outputs/.result but '{dep}' "
                    f"is not a direct declared dependency (needs: {sorted(deps)}). "
                    f"Indirect needs references are invalid (R1.7); pass the value "
                    f"through a declared dependency."
                )


def _runtime_file_lines(run: str):
    """Yield (line_text, is_write) for every consuming `.runtime/` mention.

    Pure variable assignments (`evidence_dir=".runtime/..."`) are write-pipeline
    precursors, not reads, so they are skipped.
    """
    for ln in run.splitlines():
        if ".runtime/" not in ln:
            continue
        stripped = ln.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            # Skip plain path assignments (write-pipeline precursors); a
            # command-substitution assignment that reads the file is a read.
            if not any(
                t in stripped
                for t in ("$(", "cat ", "jq ", "< ", "head ", "tail ", "sha256sum", "python ")
            ):
                continue
        is_write = (">>" in ln) or ("> " in ln) or ("mkdir -p" in ln)
        yield stripped, is_write


def validate_cross_job_file_consumption(workflow: dict, result: ValidationResult) -> None:
    """Validate cross-job file consumption uses declared artifacts/outputs.

    Rule (audit R1.6): runner-local files under `.runtime/` are NOT shared
    across jobs. A job that READS such a path must either produce it in the
    same job or declare an artifact download (upload-artifact/download-artifact
    pair). A read without declared transport is invalid.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue

        has_download = any(
            isinstance(st, dict)
            and isinstance(st.get("uses"), str)
            and "download-artifact" in st["uses"]
            for st in steps
        )
        writes_same_job = False
        reads: List[str] = []
        for st in steps:
            if not isinstance(st, dict):
                continue
            run = st.get("run")
            if not isinstance(run, str):
                continue
            for text, is_write in _runtime_file_lines(run):
                if is_write:
                    writes_same_job = True
                else:
                    reads.append(text)
        if reads and not (writes_same_job or has_download):
            result.add_error(
                f"Job '{job_name}' reads runner-local path(s) under .runtime/ that "
                f"are not produced in the same job and no artifact download is "
                f"declared (R1.6): {reads[0]!r}. Cross-job evidence must move via "
                f"declared artifacts/outputs."
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
    """True if a tag looks like a service release shape (<word>-...)."""
    return bool(re.match(r"^[a-z][a-z0-9_]*-", tag))


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
            # Reusable-workflow call jobs (`uses`) may declare `secrets: inherit`.
            if not (isinstance(secrets, str) and secrets == "inherit" and job.get("uses")):
                result.add_error(
                    f"Job '{job_name}' secrets block must be a mapping "
                    f"(shape check only); got {type(secrets).__name__}."
                )

    # Reusable workflow secrets (on.workflow_call.secrets): must be a mapping when present.
    on = _on_mapping(workflow.get("on"))
    workflow_call = on.get("workflow_call")
    if isinstance(workflow_call, dict):
        wf_call_secrets = workflow_call.get("secrets")
        if wf_call_secrets is not None and not isinstance(wf_call_secrets, dict):
            result.add_error(
                f"Workflow '{filename}' on.workflow_call.secrets must be a mapping "
                f"(shape check only); got {type(wf_call_secrets).__name__}."
            )


def _is_reusable(workflow: dict) -> bool:
    """True if the workflow exposes ``workflow_call`` (a reusable workflow)."""
    on = _on_mapping(workflow.get("on"))
    return "workflow_call" in on


def _job_uses_aws_oidc(job: dict) -> bool:
    """True if a job calls ``configure-aws-credentials`` with ``role-to-assume``.

    OIDC token acquisition via ``aws-actions/configure-aws-credentials``
    requires ``permissions.id-token: write`` on the calling workflow. A step
    that assumes an AWS role is the deploy-path signal.
    """
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    for st in steps:
        if not isinstance(st, dict):
            continue
        uses = st.get("uses")
        with_ = st.get("with") if isinstance(st.get("with"), dict) else {}
        if (
            isinstance(uses, str)
            and uses.startswith("aws-actions/configure-aws-credentials@")
            and "role-to-assume" in with_
        ):
            return True
    return False


def _job_updates_ecs_service(job: dict) -> bool:
    """True if a job mutates an ECS service (a deployment/promotion leg)."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    return any(
        isinstance(st, dict) and isinstance(st.get("run"), str) and "aws ecs update-service" in st["run"]
        for st in steps
    )


def validate_reusable_secret_expressions(workflow: dict, result: ValidationResult) -> None:
    """Reject bare literal values in a reusable-workflow call's ``secrets:`` map.

    Rule (audit B1): GitHub resolves reusable-workflow ``secrets:`` values as
    expressions. A bare literal string (e.g. ``AWS_ROLE_ARN_DEV``) is passed
    as literal text, so the called workflow receives an unusable value and the
    deployment leg fails before AWS work begins. Every value must be a
    ``${{ ... }}`` expression. ``secrets: inherit`` remains allowed.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        secrets = job.get("secrets")
        if not isinstance(uses, str) or not uses.startswith("./.github/workflows/"):
            continue
        if secrets is None:
            continue
        if isinstance(secrets, str):
            # `secrets: inherit` is valid; any other bare string is a literal.
            if secrets == "inherit":
                continue
            result.add_error(
                f"Job '{job_name}' passes secrets to reusable workflow '{uses}' "
                f"as a bare literal ('{secrets}'). Use 'secrets: inherit' or a "
                f"mapping of '${{{{ secrets.<NAME> }}}}' expressions (B1)."
            )
            continue
        if not isinstance(secrets, dict):
            continue
        for key, value in secrets.items():
            if not isinstance(value, str) or not value.startswith("${{"):
                result.add_error(
                    f"Job '{job_name}' passes secret '{key}' to reusable workflow "
                    f"'{uses}' as a literal ('{value}'), not a secret expression. "
                    f"Use '${{{{ secrets.<NAME> }}}}' (B1)."
                )


def validate_reusable_oidc_permissions(workflow: dict, result: ValidationResult) -> None:
    """Require ``id-token: write`` on reusable workflows that assume an AWS role.

    Rule (audit B1): when a workflow sets ``permissions`` explicitly, anything
    omitted defaults to ``none``. ``aws-actions/configure-aws-credentials``
    with ``role-to-assume`` therefore fails without ``permissions.id-token:
    write``. This applies to reusable deploy workflows (the called side of the
    reusable-deployment contract).
    """
    if not _is_reusable(workflow):
        return

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    uses_oidc = any(_job_uses_aws_oidc(job) for job in jobs.values() if isinstance(job, dict))
    if not uses_oidc:
        return

    perms = workflow.get("permissions")
    if not isinstance(perms, dict) or perms.get("id-token") != "write":
        result.add_error(
            f"Reusable workflow '{Path(result.workflow_path).name}' calls "
            f"aws-actions/configure-aws-credentials with role-to-assume but does "
            f"not grant 'permissions.id-token: write'. With explicit permissions, "
            f"omitted scopes are 'none' and OIDC token acquisition fails (B1)."
        )


def validate_reusable_deploy_environment(workflow: dict, result: ValidationResult) -> None:
    """Require a reusable ECS deploy job to bind a protected environment.

    Rule (audit B1): a reusable deployment job that updates an ECS service
    must be governed by the protected environment so the secret/approval
    boundary is preserved. The dynamic ``environment: ${{ inputs.env }}`` form
    is accepted for reusable jobs; a missing/empty binding is rejected.
    """
    if not _is_reusable(workflow):
        return

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if not _job_updates_ecs_service(job):
            continue
        env_ref = job.get("environment")
        name = None
        if isinstance(env_ref, str):
            name = env_ref
        elif isinstance(env_ref, dict):
            name = env_ref.get("name")
        if isinstance(name, str) and (name.startswith("${{") or name in SUPPORTED_ENVIRONMENT_NAMES):
            continue
        result.add_error(
            f"Reusable deploy job '{job_name}' updates an ECS service without a "
            f"protected environment binding. Add 'environment: ${{{{ inputs.env }}}}' "
            f"so the protected-environment secret/approval boundary is preserved (B1)."
        )


def validate_no_container_image_override(workflow: dict, result: ValidationResult) -> None:
    """Reject ECS ``RunTask`` container overrides that set the container ``image``.

    Rule (audit B2): ``containerOverrides[].image`` is not a supported ECS
    RunTask override field — image identity belongs to the task definition. A
    migration/promotion step must register a candidate-image task-definition
    revision and ``run-task`` against that revision instead.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            if "containerOverrides" in run and re.search(r'"image"\s*:', run):
                result.add_error(
                    f"Job '{job_name}' step {idx} sets the container 'image' via "
                    f"containerOverrides. ECS RunTask container overrides do NOT "
                    f"support 'image' (B2). Register a task-definition revision "
                    f"whose container image is the candidate digest, then run-task "
                    f"against that revision."
                )


def validate_smoke_readiness(workflow: dict, result: ValidationResult) -> None:
    """Reject deployment/promotion smoke URLs that target liveness.

    Rule (audit B3): deployment/promotion eligibility must consume the
    readiness endpoint ``/api/v1/health/ready`` (HTTP 200 only when ready,
    503 otherwise). ``/health/live`` is process-liveness only and must not be
    used as a smoke/eligibility signal.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for line in run.splitlines():
                if "api/v1/health/live" not in line:
                    continue
                # Flag any smoke-URL assignment or path-strip that encodes the
                # liveness suffix as a smoke/eligibility target.
                if "smoke_url" in line.lower():
                    result.add_error(
                        f"Job '{job_name}' step {idx} uses a smoke URL targeting "
                        f"/api/v1/health/live. Deployment/promotion eligibility "
                        f"must use /api/v1/health/ready (HTTP 200 only when ready); "
                        f"/health/live is process-liveness only (B3)."
                    )


def _actions_permission_granted(perms: object) -> bool:
    """True if an effective permissions block grants ``actions`` read/write.

    ``read-all`` / ``write-all`` grant every read/write scope. An explicit
    mapping grants only the keys it lists — per GitHub, once ``permissions`` is
    declared, omitted scopes become ``none``. ``None`` (undeclared) falls back
    to the repository default and is not the N1 failure mode.
    """
    if perms is None:
        return True
    if isinstance(perms, str):
        return perms in {"read-all", "write-all"}
    if isinstance(perms, dict):
        return perms.get("actions") in {"read", "write"}
    return True


def validate_gh_api_actions_permission(workflow: dict, result: ValidationResult) -> None:
    """Require effective ``actions: read`` for governed Actions/Environment API calls.

    Rule (re-review N1): ``gh api`` calls to Actions endpoints (``/actions/…``)
    and Environment endpoints (``/environments/…``) require the Actions
    repository permission. A workflow that declares an explicit ``permissions``
    block makes every omitted scope ``none``; without ``actions: read`` the
    governed check cannot authenticate at runtime even though PR CI passes.
    """
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return

    wf_perms = workflow.get("permissions")

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str) or "gh api" not in run:
                continue
            # Only Actions/Environment governance endpoints need actions: read.
            if "/actions/" not in run and "/environments/" not in run:
                continue
            effective = job.get("permissions") if job.get("permissions") is not None else wf_perms
            if _actions_permission_granted(effective):
                continue
            result.add_error(
                f"Job '{job_name}' step {idx} calls a governed Actions/Environment "
                f"API via `gh api` but the effective GITHUB_TOKEN permissions lack "
                f"'actions: read'. With explicit permissions, omitted scopes are "
                f"'none'. Add 'actions: read' to the workflow/job permissions (N1)."
            )


def validate_superseded_no_deploy_entrypoint(workflow: dict, result: ValidationResult) -> None:
    """A workflow marked SUPERSEDED / DO NOT EXECUTE must not expose a deploy path.

    Rule (re-review N2): a workflow whose header declares it superseded must be
    mechanically incapable of deployment — no reachable deploy mutation may
    remain (a deploy action, or a run step invoking a deploy keyword). History
    is preserved by git; a live alternate production path is not.
    """
    path = Path(result.workflow_path)
    header = ""
    if path.exists():
        try:
            header = path.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            header = ""
    name = str(workflow.get("name", ""))
    if not re.search(r"SUPERSEDED|DO NOT EXECUTE|DO-NOT-EXECUTE", header + "\n" + name):
        return

    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return
    filename = path.name
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_uses = job.get("uses")
        if isinstance(job_uses, str) and _is_deploy_uses(job_uses):
            result.add_error(
                f"Superseded workflow '{filename}' job '{job_name}' calls deploy "
                f"action '{job_uses}'. A SUPERSEDED / DO NOT EXECUTE workflow must "
                f"not expose a deploy entrypoint (N2)."
            )
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and _is_deploy_uses(uses):
                result.add_error(
                    f"Superseded workflow '{filename}' job '{job_name}' step {idx} "
                    f"uses deploy action '{uses}'. A SUPERSEDED / DO NOT EXECUTE "
                    f"workflow must not expose a deploy entrypoint (N2)."
                )
            run = step.get("run")
            if isinstance(run, str) and any(k in run.lower() for k in DEPLOY_KEYWORDS):
                result.add_error(
                    f"Superseded workflow '{filename}' job '{job_name}' step {idx} "
                    f"has a deploy command. A SUPERSEDED / DO NOT EXECUTE workflow "
                    f"must not expose a deploy entrypoint (N2)."
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
    validate_no_step_level_reusable(workflow, result)
    validate_gh_api_authentication(workflow, result)
    validate_environment_vocabulary(workflow, result)
    validate_needs_graph(workflow, result)
    validate_cross_job_file_consumption(workflow, result)
    validate_service_tags(workflow, result)
    validate_no_deploy(workflow, result)
    validate_permissions_shape(workflow, result)
    validate_reusable_secret_expressions(workflow, result)
    validate_reusable_oidc_permissions(workflow, result)
    validate_reusable_deploy_environment(workflow, result)
    validate_no_container_image_override(workflow, result)
    validate_smoke_readiness(workflow, result)
    validate_gh_api_actions_permission(workflow, result)
    validate_superseded_no_deploy_entrypoint(workflow, result)

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
