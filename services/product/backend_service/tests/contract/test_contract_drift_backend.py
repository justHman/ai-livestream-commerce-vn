"""Drift check: committed backend v1 contract artifacts match the canonical app.

Delegates to the repository-root drift gate (scripts/contracts/check.py)
scoped to backend; any diff between the committed contracts/v1/* artifacts
and freshly generated output fails the test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CHECK = REPO_ROOT / "scripts" / "contracts" / "check.py"


def test_contract_drift_backend() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK), "--scope", "backend"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"backend contract drift:\n{result.stdout}\n{result.stderr}"
