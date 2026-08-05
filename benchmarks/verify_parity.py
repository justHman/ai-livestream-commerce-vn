"""Canonical Director parity check vs recorded fixtures (task 1.77).

Aliases the canonical backend Director package
(``backend.application.director``) under the legacy ``core.director`` module
names, re-runs every scenario builder against canonical modules, and
compares outputs to the recorded fixtures. The pure-logic modules are
byte-identical copies; this proves the canonical Director reproduces the
recorded legacy behavior.

Exit 0 = all fixtures reproduced; exit 1 = any mismatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "out"
sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(REPO_ROOT / "services/product/backend_service/src"))
sys.path.insert(0, str(REPO_ROOT / "services/product/avatar_service/src"))
sys.path.insert(0, str(REPO_ROOT / "services/product/llm_service/src"))
sys.path.insert(0, str(REPO_ROOT / "services/product/tts_service/src"))

# ── alias canonical director under legacy names ─────────────────────────
import backend.application.director as _canon_pkg  # noqa: E402

_ALIASES = {
    "core.director": _canon_pkg,
    "core.director.catalog": __import__("backend.application.director.catalog", fromlist=["*"]),
    "core.director.cluster": __import__("backend.application.director.clustering", fromlist=["*"]),
    "core.director.embedder": __import__("backend.application.director.embeddings", fromlist=["*"]),
    "core.director.scorer": __import__("backend.application.director.scoring", fromlist=["*"]),
    "core.director.routing": __import__("backend.application.director.routing", fromlist=["*"]),
    "core.director.config": __import__("backend.application.director.config", fromlist=["*"]),
    "core.director.state": __import__("backend.application.director.state", fromlist=["*"]),
    "core.director.hooks": __import__("backend.application.director.hooks", fromlist=["*"]),
    "core.director.pivot": __import__("backend.application.director.pivot", fromlist=["*"]),
    "core.director.chat_queue": __import__(
        "backend.application.director.comment_buffer", fromlist=["*"]
    ),
    "core.director.director": __import__("backend.application.director.decision", fromlist=["*"]),
    "core.director.runtime": __import__(
        "backend.application.director.session_context", fromlist=["*"]
    ),
}
sys.modules.update(_ALIASES)

# Scenario builders import ``from core.director.*`` — they now get canonical.
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


def main() -> int:
    all_pass = True
    for name, builder in SCENARIOS.items():
        fixture = json.loads(
            (FIXTURES_DIR / f"{name.replace('/', '_')}.json").read_text(encoding="utf-8")
        )
        recorded = fixture["outputs"]
        recomputed = builder()["outputs"]
        if recomputed == recorded:
            print(f"{name:24s} PASS  exact match")
        else:
            all_pass = False
            print(f"{name:24s} FAIL  recorded != canonical")
            for key in sorted(set(recorded) | set(recomputed)):
                if recorded.get(key) != recomputed.get(key):
                    print(f"    - {key}: recorded={recorded.get(key)!r}")
                    print(f"      canonical={recomputed.get(key)!r}")
    print("PARITY:", "ALL PASS" if all_pass else "FAILURES PRESENT")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
