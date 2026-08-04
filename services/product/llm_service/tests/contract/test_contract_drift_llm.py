"""Drift check: committed llm v1 contract artifact matches the canonical app.

Delegates to the repository-root drift gate (scripts/contracts/check.py)
scoped to llm; any diff between the committed contracts/v1/openapi.json and
freshly generated output fails the test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CHECK = REPO_ROOT / "scripts" / "contracts" / "check.py"


def test_contract_drift_llm() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK), "--scope", "llm"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"llm contract drift:\n{result.stdout}\n{result.stderr}"
