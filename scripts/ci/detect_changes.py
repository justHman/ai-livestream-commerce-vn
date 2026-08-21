"""Return the affected service set for a git commit range (test-matrix input).

Wraps `detect_affected_areas` (path classifier) with a `git diff --name-only`
over a commit range so CI can decide which product services run which tier
of the test matrix (see scripts/ci/test_matrix.json).

Usage:
  python scripts/ci/detect_changes.py <from> <to> [--json]
  python scripts/ci/detect_changes.py --range HEAD~3..HEAD [--json]

Output (--json): {"services": [...], "areas": [...], "by_path": {...}}
`services` is the subset of PRODUCT_AREAS affected (backend_service,
llm_service, tts_service, avatar_service); contract/source-DTO changes fan
out to the exact consumers (backend + workbench for backend contracts;
owner + backend for llm/tts/avatar).

Exit code 0 always (empty range -> empty service set). Unknown/root-only
paths map to shared areas and are never treated as a product service.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import List

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.ci.detect_affected_areas import PRODUCT_AREAS, detect_affected_areas  # noqa: E402


def changed_paths(repo_root: Path, base: str, head: str) -> List[str]:
    """Names of files changed between base..head (name-only, no status)."""
    # First push of a branch reports before=0000...0 (no parent commit): list
    # the initial commit's files directly instead of a parentless diff.
    if base == "0" * 40:
        cmd = ["git", "show", "--name-only", "--format=", head]
    else:
        cmd = ["git", "diff", "--name-only", f"{base}..{head}"]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return [line for line in proc.stdout.splitlines() if line.strip()]
    # A force-push that rewrote the branch makes ``event.before`` the old
    # dangling head, which is not in the fresh clone -> invalid revision
    # range. Treat the whole head as affected (conservative) instead of
    # failing the gate (reviewer R9.8 exact-head CI; first-push-adjacent).
    fallback = subprocess.run(
        ["git", "show", "--name-only", "--format=", head],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if fallback.returncode != 0:
        raise RuntimeError(f"git diff {base}..{head} failed: {proc.stderr.strip()}")
    return [line for line in fallback.stdout.splitlines() if line.strip()]


def affected_services(repo_root: Path, base: str, head: str) -> List[str]:
    """Sorted product-service areas affected by the base..head commit range."""
    result = detect_affected_areas(changed_paths(repo_root, base, head))
    return sorted(set(result["areas"]) & set(PRODUCT_AREAS))


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect affected services for a commit range.")
    parser.add_argument("base", nargs="?", help="base commit (default: base of --range)")
    parser.add_argument("head", nargs="?", help="head commit (default: HEAD)")
    parser.add_argument("--range", help="range like HEAD~3..HEAD (overrides base/head)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if args.range:
        if ".." not in args.range:
            parser.error("--range must look like HEAD~3..HEAD")
        base, head = args.range.split("..", 1)
    else:
        base, head = args.base or "HEAD~1", args.head or "HEAD"

    try:
        paths = changed_paths(args.repo_root, base, head)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    result = detect_affected_areas(paths)
    services = sorted(set(result["areas"]) & set(PRODUCT_AREAS))
    payload = {
        "services": services,
        "areas": result["areas"],
        "by_path": result["by_path"],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(", ".join(services) if services else "(no product service affected)")


if __name__ == "__main__":
    main()
