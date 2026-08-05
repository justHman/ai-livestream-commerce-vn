"""Fixture generator entrypoint (task 1.77).

Runs every scenario against the canonical director (aliased as core.director by verify_parity) and writes
deterministic input/output JSON under ``benchmarks/fixtures/out/`` plus a
manifest. Re-running must produce byte-identical files (same Python version,
same canonical director tree).

Usage:
    python -m benchmarks.fixtures [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Ensure canonical director importable for the parity check.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services/product/backend_service/src"))

from benchmarks.fixtures.scenarios import (  # noqa: E402
    decision_preparation,
    event_persistence,
    playback,
    queue_chunking,
    session_context,
    stream_analysis,
)

SCENARIOS = {
    "session-context": session_context.scenario,
    "event/persistence": event_persistence.scenario,
    "decision-preparation": decision_preparation.scenario,
    "playback": playback.scenario,
    "queue/chunking": queue_chunking.scenario,
    "stream-analysis": stream_analysis.scenario,
}

MANIFEST_VERSION = "1.0.0"


# The fixtures characterize the LEGACY Director tree (pre-extraction, task 1.77).
# This is the supervisor base for task 1.77; do not follow repo HEAD, which
# moves as benchmark commits land.
PRODUCER_COMMIT = "fb1e5b5"


def _producer_commit() -> str:
    """Short SHA of the director tree the fixtures were produced from."""
    return PRODUCER_COMMIT


def _producer_note() -> str:
    return (
        "Legacy Director at producer commit; hash embedder; "
        "fixed corpus/clock; no randomness in outputs."
    )


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "version": MANIFEST_VERSION,
        "producer_commit": _producer_commit(),
        "producer_note": _producer_note(),
        "scenarios": {},
    }
    for name, builder in SCENARIOS.items():
        data = builder()
        payload = {"scenario": name, "inputs": data["inputs"], "outputs": data["outputs"]}
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest["scenarios"][name] = {
            "digest": digest,
            "generated_by": "python -m benchmarks.fixtures",
        }
        slug = name.replace("/", "_")
        (out_dir / f"{slug}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(SCENARIOS)} fixtures + manifest to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Director benchmark fixtures")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"))
    args = parser.parse_args()
    generate(Path(args.out))


if __name__ == "__main__":
    main()
