"""Terraform module source resolution (OpenSpec 1.50).

Moved from ``core/tests/test_canonical_path_references.py`` — Terraform
checks live in ``infra/tests/``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_terraform_module_sources_resolve_from_each_environment() -> None:
    environments = tuple((ROOT / "infra" / "environments").glob("*/main.tf"))
    assert environments

    for config in environments:
        for source in re.findall(
            r'^\s*source\s*=\s*"([^"\n]+)"', config.read_text(), re.MULTILINE
        ):
            assert (config.parent / source).resolve().is_dir(), (
                f"{config}: missing module {source}"
            )
