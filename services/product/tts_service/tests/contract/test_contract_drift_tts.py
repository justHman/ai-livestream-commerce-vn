"""Drift check: committed tts v1 contract artifact matches the canonical app.

Delegates to the repository-root drift gate (scripts/contracts/check.py)
scoped to tts; any diff between the committed contracts/v1/openapi.json and
freshly generated output fails the test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CHECK = REPO_ROOT / "scripts" / "contracts" / "check.py"


def test_contract_drift_tts() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK), "--scope", "tts"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"tts contract drift:\n{result.stdout}\n{result.stderr}"
