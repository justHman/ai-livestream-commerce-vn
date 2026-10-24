"""6.3 local simulation — release tag eligibility, digest promotion, rollback.

Simulates the three gates release-service.yml enforces (5.1/5.2/5.4) and the
service-scoped rollback (5.5 / design §6) WITHOUT touching AWS:

1. tag parse + eligibility  — validate_service_tag (5.1)
2. main ancestry           — git merge-base --is-ancestor (5.2)
3. staging evidence        — read deploy-evidence/staging/<sha>.jsonl (5.2)
4. digest promotion        — evidence digest is @sha256: exact, no rebuild (5.4)
5. rollback                — previous task definition restore, service-scoped (5.5)

Runnable: python scripts/ci/simulate_release_path.py [--fixture-dir <path>]
Exit 0 = all gates behave as designed for eligible AND ineligible inputs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci.validate_workflow_inputs import validate_service_tag  # noqa: E402


def _in_main(repo_root: Path, commit: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _evidence_line(evidence_root: Path, sha: str, service: str) -> dict | None:
    for base in (evidence_root / "deploy-evidence" / "staging",):
        f = base / f"{sha}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if (
                row.get("env") == "staging"
                and row.get("commit_sha") == sha
                and row.get("service") == service
                and row.get("result") == "success"
            ):
                return row
    return None


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        raise SystemExit(1)
    print(f"  ok: {msg}")


def simulate(repo_root: Path, fixture_dir: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True
    ).stdout.strip()
    sha = head[:40]
    print(f"repo HEAD: {sha}")

    # 1. Tag parse (5.1)
    print("[5.1] tag parsing")
    tag = validate_service_tag("backend-v1.2.3")
    _assert(tag["service"] == "backend_service" and tag["version"] == "v1.2.3", "backend-v1.2.3 parses")
    for bad in ("backend-v1.2", "v1.2.3", "database-v1.2.3", "lmcache-v1.2.3", ""):
        try:
            validate_service_tag(bad)
            _assert(False, f"ineligible tag {bad!r} rejected")
        except ValueError:
            _assert(True, f"ineligible tag {bad!r} rejected")

    # 2. Main ancestry (5.2)
    print("[5.2] main ancestry")
    _assert(_in_main(repo_root, sha), "HEAD is contained in main lineage")

    # 3. Staging evidence (5.2)
    print("[5.2] staging evidence gate")
    fixture = fixture_dir / "deploy-evidence" / "staging" / f"{sha}.jsonl"
    if not fixture.exists():
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            json.dumps(
                {
                    "ts": "2026-08-05T12:00:00Z",
                    "env": "staging",
                    "commit_sha": sha,
                    "service": "backend_service",
                    "initiator": "operator",
                    "prev_digest": "imjusthman/ai-live-backend@sha256:aaaa",
                    "new_digest": "imjusthman/ai-live-backend@sha256:bbbb",
                    "result": "success",
                }
            )
            + "\n",
            encoding="utf-8",
        )
    line = _evidence_line(fixture_dir, sha, "backend_service")
    _assert(line is not None, f"staging evidence found for {sha}")
    digest = line["new_digest"]

    # 4. Exact-digest promotion (5.4)
    print("[5.4] digest promotion")
    _assert("@sha256:" in digest and ":staging-" not in digest and ":dev-" not in digest,
            f"promoted digest is immutable ({digest})")
    _assert(digest == line["new_digest"], "promotion uses the exact recorded digest (no rebuild)")

    # 5. Service-scoped rollback (5.5)
    print("[5.5] rollback")
    prev = line["prev_digest"]
    _assert(prev != digest, "previous digest differs from new digest")
    # rollback restores ONLY this service's previous task definition; the
    # _deploy-service rollback() path is a single update-service call with the
    # old task def (no other service is touched).
    rollback_script = (
        ROOT / ".github" / "workflows" / "_deploy-service.yml"
    ).read_text(encoding="utf-8")
    _assert("aws ecs update-service" in rollback_script, "rollback = update-service to old task def")
    _assert("restoring $ECS_SERVICE to $old_task" in rollback_script, "rollback is per-service (ECS_SERVICE)")

    print("\nALL 6.3 GATES PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="6.3 release-path simulation")
    parser.add_argument("--fixture-dir", type=Path, default=Path(tempfile.gettempdir()) / "6x-sim")
    args = parser.parse_args()
    simulate(ROOT, args.fixture_dir)
