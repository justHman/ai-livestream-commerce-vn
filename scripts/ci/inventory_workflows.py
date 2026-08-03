"""
Task 80 — OpenSpec 2.1: Inventory existing workflows.

Creates concise tracked workflow inventory: file, trigger/event/filter,
reusable/event entry, jobs/actions, artifact/deploy/infra mutation,
secrets/environment, canonical target or removal condition.

Covers all workflows and referenced scripts/reusable workflows.
Identifies implicit deploys, path-skipped required checks, stale service
paths and overlapping release paths without changing triggers yet.

Add structural validation that every workflow is classified and references exist.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Shared GHA-safe YAML loader (same-directory import)
import importlib.util as _util

_gha_yaml = _util.spec_from_file_location(
    "_gha_yaml",
    Path(__file__).resolve().parent / "_gha_yaml.py",
)
_mod = _util.module_from_spec(_gha_yaml)
_gha_yaml.loader.exec_module(_mod)
load_yaml = _mod.load_file

# ── Inventory schema ────────────────────────────────────────────────────────


def _get_triggers(on: Any) -> List[Dict[str, Any]]:
    """Extract structured trigger list from the 'on' key."""
    if on is None:
        return []
    if isinstance(on, str):
        return [{"event": on, "filter": {}}]
    if isinstance(on, list):
        return [{"event": e, "filter": {}} for e in on if isinstance(e, str)]
    if isinstance(on, dict):
        triggers = []
        for event_name, filters in on.items():
            if isinstance(filters, dict):
                triggers.append({"event": event_name, "filter": filters})
            else:
                triggers.append({"event": event_name, "filter": {}})
        return triggers
    return []


def _get_jobs_snapshot(workflow: dict) -> List[Dict[str, Any]]:
    """Extract job names, actions/uses, and key behaviors."""
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    snapshot = []
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        entry = {"name": name, "runs_on": job.get("runs-on"), "uses": [], "key_actions": []}
        steps = job.get("steps", [])
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    uses = step.get("uses")
                    if uses:
                        entry["uses"].append(uses)
                    run = step.get("run", "")
                    if isinstance(run, str) and run.strip():
                        entry["key_actions"].append(run.strip()[:80])
        snapshot.append(entry)
    return snapshot


def inventory_workflow(path: Path) -> Dict[str, Any]:
    """Inventory a single workflow file."""
    wf = load_yaml(path)
    if wf is None or not isinstance(wf, dict):
        return {
            "file": str(path),
            "parse_error": True,
            "triggers": [],
            "jobs": [],
            "permissions": None,
            "env": None,
            "concurrency": None,
        }

    triggers = _get_triggers(wf.get("on"))
    jobs = _get_jobs_snapshot(wf)

    return {
        "file": str(path),
        "name": wf.get("name", Path(path).stem),
        "triggers": triggers,
        "jobs": jobs,
        "permissions": wf.get("permissions"),
        "env": list(wf.get("env", {}).keys()) if isinstance(wf.get("env"), dict) else [],
        "concurrency": wf.get("concurrency"),
    }


def inventory_all(workflows_dir: Path) -> List[Dict[str, Any]]:
    """Inventory all workflows in a directory."""
    results = []
    for f in sorted(workflows_dir.glob("*.yml")):
        results.append(inventory_workflow(f))
    return results


def generate_report(workflows_dir: Path) -> str:
    """Generate a human-readable inventory report."""
    inventory = inventory_all(workflows_dir)
    lines = []
    lines.append("# Workflow Inventory — OpenSpec 2.1")
    lines.append("")
    lines.append(f"Generated: {__import__('datetime').datetime.now().isoformat()}")
    lines.append(f"Workflows directory: {workflows_dir}")
    lines.append("")
    lines.append("---")

    for wf in inventory:
        if wf.get("parse_error"):
            lines.append(f"\n## {wf['file']}")
            lines.append("**PARSE ERROR** — file could not be parsed as YAML mapping.")
            continue

        lines.append(f"\n## {wf['file']}")
        lines.append(f"- Name: {wf['name']}")
        lines.append(f"- Permissions: {wf['permissions']}")
        if wf.get("env"):
            lines.append(f"- Env vars: {', '.join(wf['env'])}")
        if wf.get("concurrency"):
            lines.append(f"- Concurrency: {wf['concurrency']}")

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
            if job.get("uses"):
                for u in job["uses"]:
                    lines.append(f"    - uses: `{u}`")
            if job.get("key_actions"):
                lines.append("    - key actions:")
                for a in job["key_actions"]:
                    lines.append(f"      - `{a}`")

    lines.append("\n---")
    lines.append(f"\nTotal: {len(inventory)} workflow(s) inventoried.")
    if any(wf.get("parse_error") for wf in inventory):
        lines.append("\n**WARNING**: Some workflows failed to parse. Check parse_error fields.")

    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Inventory GitHub Actions workflows.")
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=Path(".github/workflows"),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write report to this file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON inventory",
    )

    args = parser.parse_args()
    workflows_dir = args.repo_root / args.workflows_dir

    if not workflows_dir.exists():
        print(f"ERROR: Workflows directory not found: {workflows_dir}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        inv = inventory_all(workflows_dir)
        print(json.dumps(inv, indent=2))
    else:
        report = generate_report(workflows_dir)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report, encoding="utf-8")
            print(f"Report written to {args.report}")
        else:
            print(report)


if __name__ == "__main__":
    main()
