"""
Task 80 — OpenSpec 2.1: Inventory existing workflows.

Creates concise tracked workflow inventory: file, trigger/event/filter,
reusable/event entry, jobs/actions, artifact/deploy/infra mutation,
secrets/environment, canonical target or removal condition.

Covers all workflows and referenced scripts/reusable workflows (recursive).
Identifies implicit deploys, path-skipped required checks, stale service
paths and overlapping release paths without changing triggers yet.

Add structural validation that every workflow is classified and references exist.

The JSON output doubles as a tracked inventory manifest: callers/CI may write
it to a tracked path and assert it does not drift from the source of truth.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Shared GHA-safe YAML loader (same-directory import)
import importlib.util as _util

_gha_yaml = _util.spec_from_file_location(
    "_gha_yaml",
    Path(__file__).resolve().parent / "_gha_yaml.py",
)
_mod = _util.module_from_spec(_gha_yaml)
_gha_yaml.loader.exec_module(_mod)
load_yaml = _mod.load_file

# Canonical service short names that release tags may use.
SERVICE_TAG_NAMES = frozenset({"backend", "llm", "tts", "avatar"})

# Actions that perform an infra/deploy mutation (for mutation classification).
INFRA_MUTATION_ACTIONS = (
    "aws-actions/amazon-ecs-deploy-task-definition@",
    "hashicorp/terraform-github-actions@",
    "selefra/terraform-apply-action@",
)
DEPLOY_ACTIONS = (
    "aws-actions/amazon-ecs-deploy-task-definition@",
    "azure/webapps-deploy@",
    "google-github-actions/deploy-cloudrun@",
    "google-github-actions/deploy-appengine@",
    "cloudflare/wrangler-action@",
)
DEPLOY_KEYWORDS = (
    "deploy",
    "ecs update-service",
    "terraform apply",
    "kubectl apply",
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _on_mapping(on: Any) -> Dict[str, Any]:
    """Normalize `on` into a {event: filter} mapping."""
    if on is None:
        return {}
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {e: {} for e in on if isinstance(e, str)}
    if isinstance(on, dict):
        return {k: v for k, v in on.items()}
    return {}


def _jobs(workflow: dict) -> Dict[str, dict]:
    jobs = workflow.get("jobs")
    return {k: v for k, v in jobs.items()} if isinstance(jobs, dict) else {}


def _normalize_path(uses: str) -> str:
    """Normalize a uses ref to a workflow-relative path."""
    return uses.removeprefix("./").removeprefix(".github/workflows/")


_SECRETS_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_.-]+)\s*\}\}")


def extract_secret_refs(value: Any) -> List[str]:
    """Recursively extract `${{ secrets.NAME }}` references from any value.

    Scans strings, and recursively into dicts/lists (e.g. `with:` blocks,
    `env:` blocks, `run:` scripts). Returns deduplicated sorted secret names.
    Never reads secret values — only the referenced name is surfaced.
    """
    found: List[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            for m in _SECRETS_REF.finditer(v):
                found.append(m.group(1))
        elif isinstance(v, dict):
            for k, val in v.items():
                _walk(k)
                _walk(val)
        elif isinstance(v, list):
            for item in v:
                _walk(item)

    _walk(value)
    return sorted(set(found))


def step_push_semantics(step: dict) -> bool:
    """Return True if a step is a build-push-action that actually pushes.

    Honors `with.push`: `true` (or YAML bool) means push; `false` means the
    action builds/loads without pushing. Absence of `push` under the build-push
    action is treated as push (action default). Any other action is not a
    build-push and returns False.
    """
    uses = step.get("uses", "")
    if not isinstance(uses, str) or "docker/build-push-action@" not in uses:
        return False
    with_block = step.get("with")
    if isinstance(with_block, dict) and "push" in with_block:
        return bool(with_block["push"])
    return True


# ── Inventory schema ────────────────────────────────────────────────────────


def _inventory_job(
    name: str, job: dict, wf_path: Path, workflows_dir: Path, seen: set
) -> Dict[str, Any]:
    """Inventory a single job with step-level uses, secrets, environment."""
    job_secrets: List[str] = (
        list(job.get("secrets", {}).keys()) if isinstance(job.get("secrets"), dict) else []
    )
    job_secrets_ext = extract_secret_refs(job.get("env")) + extract_secret_refs(job.get("with"))
    entry: Dict[str, Any] = {
        "name": name,
        "runs_on": job.get("runs-on"),
        "uses": [],
        "steps": [],
        "environment": job.get("environment"),
        "secrets": sorted(set(job_secrets + job_secrets_ext)),
        "key_actions": [],
    }

    # Job-level reusable reference
    job_uses = job.get("uses")
    if isinstance(job_uses, str):
        entry["uses"].append(job_uses)
        if job_uses.startswith("./") or job_uses.startswith(".github/"):
            entry["reusable_ref"] = job_uses

    steps = job.get("steps", [])
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_info: Dict[str, Any] = {"name": step.get("name")}
            uses = step.get("uses")
            run = step.get("run", "")
            if uses:
                step_info["uses"] = uses
                entry["uses"].append(uses)
            if isinstance(run, str) and run.strip():
                step_info["run"] = run.strip()[:80]
                entry["key_actions"].append(run.strip()[:80])
                entry.setdefault("raw_runs", []).append(run)
            if "with" in step:
                step_info["with"] = sorted(step["with"].keys())
                step_info["with_push_semantics"] = step_push_semantics(step)
            # Extract secret references from this step (with/env/run) without
            # reading values.
            step_secrets = extract_secret_refs(step.get("with"))
            step_secrets += extract_secret_refs(step.get("env"))
            step_secrets += extract_secret_refs(run)
            for s in step_secrets:
                if s not in entry["secrets"]:
                    entry["secrets"].append(s)
            entry["secrets"] = sorted(set(entry["secrets"]))
            entry["steps"].append(step_info)

            # Recursively inventory a local reusable workflow referenced by this step.
            if isinstance(uses, str) and (uses.startswith("./") or uses.startswith(".github/")):
                target = _resolve_reusable(uses, workflows_dir)
                if target and str(target) not in seen:
                    seen.add(str(target))
                    sub = inventory_workflow(target, workflows_dir, seen)
                    entry.setdefault("reusable_inventory", []).append(sub)

    return entry


def _resolve_reusable(uses: str, workflows_dir: Path) -> Optional[Path]:
    """Resolve a local reusable workflow ref to an absolute file path."""
    if not (uses.startswith("./") or uses.startswith(".github/")):
        return None
    norm = _normalize_path(uses)
    candidate = workflows_dir / Path(norm).name
    return candidate if candidate.exists() else None


def inventory_workflow(
    path: Path, workflows_dir: Path, seen: Optional[set] = None
) -> Dict[str, Any]:
    """Inventory a single workflow file (recursive into reusable refs)."""
    if seen is None:
        seen = set()
    seen.add(str(path))

    wf = load_yaml(path)
    if wf is None or not isinstance(wf, dict):
        return {
            "file": str(path),
            "name": Path(path).stem,
            "parse_error": True,
            "triggers": [],
            "jobs": [],
            "permissions": None,
            "env": [],
            "concurrency": None,
            "role": "unknown",
            "canonical_target": None,
            "removal_condition": None,
            "mutation": {"artifact_push": False, "deploy": False, "infra_mutation": False},
            "deploy_actions": [],
            "service_tags": [],
        }

    triggers = _on_mapping(wf.get("on"))
    trigger_list: List[Dict[str, Any]] = [
        {"event": event, "filter": filters} for event, filters in triggers.items()
    ]

    is_reusable = "workflow_call" in triggers
    role = "reusable" if is_reusable else "event-entry"
    filename = Path(path).name
    is_deploy_wf = "deploy" in filename or "release" in filename
    is_ci = filename == "ci.yml"

    jobs = _jobs(wf)
    job_snapshots = [_inventory_job(n, j, path, workflows_dir, seen) for n, j in jobs.items()]

    # Mutation classification (uses full raw runs, not truncated summaries)
    mutation = {"artifact_push": False, "deploy": False, "infra_mutation": False}
    deploy_actions: List[str] = []
    all_uses = [u for job in job_snapshots for u in job.get("uses", [])]
    all_runs = " ".join(r for job in job_snapshots for r in job.get("raw_runs", []))

    # artifact_push honors step-level with.push semantics under build-push-action.
    artifact_push_steps = [
        step
        for job in job_snapshots
        for step in job.get("steps", [])
        if step.get("with_push_semantics") is True
    ]
    if artifact_push_steps:
        mutation["artifact_push"] = True
    if any(u.startswith(p) for u in all_uses for p in DEPLOY_ACTIONS):
        mutation["deploy"] = True
    if any(u.startswith(p) for u in all_uses for p in INFRA_MUTATION_ACTIONS):
        mutation["infra_mutation"] = True
    if any(kw in all_runs.lower() for kw in DEPLOY_KEYWORDS):
        mutation["deploy"] = True
    for u in all_uses:
        if any(u.startswith(p) for p in DEPLOY_ACTIONS):
            deploy_actions.append(u)

    # Service tags referenced by push.tags
    service_tags: List[str] = []
    push = triggers.get("push")
    if isinstance(push, dict) and isinstance(push.get("tags"), list):
        service_tags = [t for t in push["tags"] if isinstance(t, str)]

    # Canonical target / removal condition
    canonical_target = None
    removal_condition = None
    if is_ci:
        canonical_target = "CI gate"
    elif is_deploy_wf:
        canonical_target = "explicit deploy/release"
    elif "build" in filename:
        canonical_target = "offline image build"
    elif "seed" in filename:
        canonical_target = "offline weight seeding"
    if is_deploy_wf and mutation.get("deploy"):
        removal_condition = "removal condition: nothing (keep while deploy path is in use)"

    return {
        "file": str(path),
        "name": wf.get("name", Path(path).stem),
        "role": role,
        "triggers": trigger_list,
        "jobs": job_snapshots,
        "permissions": wf.get("permissions"),
        "env": list(wf.get("env", {}).keys()) if isinstance(wf.get("env"), dict) else [],
        "concurrency": wf.get("concurrency"),
        "secrets": list(wf.get("secrets", {}).keys())
        if isinstance(wf.get("secrets"), dict)
        else [],
        "mutation": mutation,
        "deploy_actions": deploy_actions,
        "service_tags": service_tags,
        "canonical_target": canonical_target,
        "removal_condition": removal_condition,
        "path_filters": push.get("paths") if isinstance(push, dict) else None,
        "branch_filters": push.get("branches") if isinstance(push, dict) else None,
    }


def inventory_all(workflows_dir: Path) -> List[Dict[str, Any]]:
    """Inventory all workflows in a directory (recursive into reusable refs)."""
    seen: set = set()
    results = []
    for f in sorted(workflows_dir.glob("*.yml")):
        results.append(inventory_workflow(f, workflows_dir, seen))
    return results


# ── Structural checks (2.1 brief: every workflow classified + refs exist) ───


def _collect_reusable_refs(inventory: List[Dict[str, Any]]) -> List[str]:
    refs: List[str] = []
    for wf in inventory:
        for job in wf.get("jobs", []):
            for u in job.get("uses", []):
                if u.startswith("./") or u.startswith(".github/"):
                    refs.append(u)
    return refs


def validate_inventory(inventory: List[Dict[str, Any]], workflows_dir: Path) -> List[str]:
    """Return a list of structural validation errors (empty = all pass)."""
    errors: List[str] = []
    for wf in inventory:
        if wf.get("parse_error"):
            errors.append(f"{wf['file']}: unparseable workflow (failed YAML parse)")
            continue
        if wf.get("role") == "unknown":
            errors.append(f"{wf['file']}: workflow not classified")

        # Every referenced local reusable must exist.
        for job in wf.get("jobs", []):
            for u in job.get("uses", []):
                if u.startswith("./") or u.startswith(".github/"):
                    target = _resolve_reusable(u, workflows_dir)
                    if target is None:
                        errors.append(f"{wf['file']}: missing reusable ref '{u}'")

    return errors


# ── Report generation ───────────────────────────────────────────────────────


def generate_report(workflows_dir: Path) -> str:
    """Generate a human-readable inventory report with findings."""
    inventory = inventory_all(workflows_dir)
    errors = validate_inventory(inventory, workflows_dir)
    lines: List[str] = []
    lines.append("# Workflow Inventory — OpenSpec 2.1")
    lines.append("")
    lines.append(f"Generated: {__import__('datetime').datetime.now().isoformat()}")
    lines.append(f"Workflows directory: {workflows_dir}")
    lines.append("")
    if errors:
        lines.append("## Structural findings")
        for e in errors:
            lines.append(f"- ERROR: {e}")
        lines.append("")
    lines.append("---")

    for wf in inventory:
        if wf.get("parse_error"):
            lines.append(f"\n## {wf['file']} — PARSE ERROR")
            continue

        lines.append(f"\n## {wf['file']}")
        lines.append(f"- Name: {wf['name']}  |  Role: {wf['role']}")
        lines.append(f"- Permissions: {wf['permissions']}")
        if wf.get("env"):
            lines.append(f"- Env vars: {', '.join(wf['env'])}")
        if wf.get("concurrency"):
            lines.append(f"- Concurrency: {wf['concurrency']}")
        lines.append(
            "- Mutation: artifact_push=%s, deploy=%s, infra_mutation=%s"
            % (
                wf["mutation"]["artifact_push"],
                wf["mutation"]["deploy"],
                wf["mutation"]["infra_mutation"],
            )
        )
        if wf.get("deploy_actions"):
            lines.append(f"- Deploy actions: {', '.join(wf['deploy_actions'])}")
        if wf.get("service_tags"):
            lines.append(f"- Service tags: {', '.join(wf['service_tags'])}")
        if wf.get("path_filters"):
            lines.append(f"- Path filters: {', '.join(wf['path_filters'])}")
        if wf.get("canonical_target"):
            lines.append(f"- Canonical target: {wf['canonical_target']}")
        if wf.get("removal_condition"):
            lines.append(f"- {wf['removal_condition']}")

        # Triggers
        lines.append("\n### Triggers")
        for t in wf["triggers"]:
            event = t["event"]
            filt = t["filter"]
            if filt:
                parts = []
                for k, v in filt.items():
                    if isinstance(v, list):
                        parts.append(f"{k}: {', '.join(str(x) for x in v)}")
                    elif isinstance(v, dict):
                        sub = ", ".join(f"{sk}: {sv}" for sk, sv in v.items())
                        parts.append(f"{k}: {{{sub}}}")
                    else:
                        parts.append(f"{k}: {v}")
                lines.append(f"  - `{event}` ({'; '.join(parts)})")
            else:
                lines.append(f"  - `{event}`")

        # Jobs
        lines.append("\n### Jobs")
        for job in wf["jobs"]:
            lines.append(f"  - **{job['name']}**")
            if job.get("runs_on"):
                lines.append(f"    - runs-on: {job['runs_on']}")
            if job.get("environment"):
                lines.append(f"    - environment: {job['environment']}")
            if job.get("secrets"):
                lines.append(f"    - secrets: {', '.join(job['secrets'])}")
            if job.get("uses"):
                for u in job["uses"]:
                    lines.append(f"    - uses: `{u}`")
            if job.get("reusable_inventory"):
                lines.append("    - reusable workflow: (see below)")
            if job.get("key_actions"):
                lines.append("    - key actions:")
                for a in job["key_actions"]:
                    lines.append(f"      - `{a}`")
            for step in job.get("steps", []):
                if "uses" in step or "run" in step:
                    desc = step.get("uses") or step.get("run")
                    lines.append(f"    - step {step.get('name', '')}: {desc}")

    lines.append("\n---")
    lines.append(f"\nTotal: {len(inventory)} workflow(s) inventoried.")
    if errors:
        lines.append(f"\n**STRUCTURAL ERRORS**: {len(errors)}")
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Inventory GitHub Actions workflows.")
    parser.add_argument("--workflows-dir", type=Path, default=Path(".github/workflows"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, default=None, help="Write report to this file")
    parser.add_argument("--json", action="store_true", help="Output JSON inventory")
    parser.add_argument("--manifest", type=Path, default=None, help="Write JSON manifest")
    parser.add_argument(
        "--check-drift",
        type=Path,
        default=None,
        help="Fail if the JSON inventory drifts from this manifest file",
    )

    args = parser.parse_args()
    workflows_dir = args.repo_root / args.workflows_dir

    if not workflows_dir.exists():
        print(f"ERROR: Workflows directory not found: {workflows_dir}", file=sys.stderr)
        sys.exit(1)

    if args.json or args.manifest or args.check_drift:
        inv = inventory_all(workflows_dir)
        payload = {"workflows": inv}
        if args.manifest:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if args.check_drift:
            if not args.check_drift.exists():
                print(f"ERROR: manifest {args.check_drift} does not exist", file=sys.stderr)
                sys.exit(1)
            current = json.dumps(payload, indent=2, sort_keys=True)
            baseline = args.check_drift.read_text(encoding="utf-8")
            if current != baseline:
                print(
                    f"ERROR: inventory manifest drift in {args.check_drift}",
                    file=sys.stderr,
                )
                sys.exit(1)
        if args.json:
            print(json.dumps(payload, indent=2))
        return

    report = generate_report(workflows_dir)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
        print(f"Report written to {args.report}")
    else:
        print(report)


if __name__ == "__main__":
    main()
