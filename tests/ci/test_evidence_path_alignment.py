"""R1.6 anti-regression: cross-job deployment evidence stays aligned.

Regression for the F.12 catch where ``record-evidence`` read a nested path
``$adir/deploy/evidence/staging/<sha>.jsonl`` while upload-artifact@v4 stores a
non-glob single file at the artifact root (``<sha>.jsonl``). A mismatch makes
the evidence silently empty and the release chain permanently blocked.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Single-file evidence uploads name the artifact with a per-leg suffix and give
# ``path:`` a concrete file, which upload-artifact@v4 stores at artifact root.
EVIDENCE_UPLOAD = re.compile(r"deploy-evidence-\S+\.jsonl")
EVIDENCE_READ_BASENAME = re.compile(r"\$adir/\$[A-Z_]+\$?\{?\}?[^/]*\.jsonl")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _evidence_consumers() -> list[tuple[str, str]]:
    """(workflow, run-step-text) pairs that read downloaded evidence files."""
    consumers: list[tuple[str, str]] = []
    for name in ("deploy-staging.yml", "release-service.yml"):
        doc = _load(name)
        for _job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if re.search(r"ef=\".*\$adir", run):
                    consumers.append((name, run))
    return consumers


def test_every_evidence_read_targets_artifact_root_basename() -> None:
    # Every ``$adir`` evidence read must address the uploaded single file at the
    # artifact root — never a path nested under the artifact subdirectory.
    consumers = _evidence_consumers()
    assert consumers, "expected at least one $adir evidence consumer"
    for name, run in consumers:
        assert EVIDENCE_READ_BASENAME.search(run), (
            f"{name}: evidence read does not use $adir/<sha>.jsonl at artifact root: {run!r}"
        )


def test_every_evidence_upload_is_a_single_concrete_file() -> None:
    # ``path:`` must be a concrete file (artifact root storage), not a glob/dir.
    doc = _load("_deploy-service.yml")
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            with_ = step.get("with") or {}
            upload_path = with_.get("path", "") or ""
            if "deploy-evidence" in upload_path:
                assert EVIDENCE_UPLOAD.search(upload_path), (
                    f"evidence upload path is not a single file: {upload_path!r}"
                )
