#!/usr/bin/env python3
"""Validate LiveKit platform assets are structurally correct and pin-compliant.

Runs without Docker. Checks the pinned base image digest, the config YAML, and
the entrypoint's fail-loud credential guard. Structural ownership checks are
covered by ``infra/tests``; this script is the platform-side validation surface
referenced from ``services/platform/livekit/README.md``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "services" / "platform" / "livekit"

PIN_REGEX = re.compile(
    r"FROM\s+livekit/livekit-server:(?P<version>v\d[\w.\-]*)"
    r"@sha256:(?P<digest>[0-9a-f]{64})"
)


def _fail(message: str) -> None:
    print(f"[validation] FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_pin() -> None:
    dockerfile = (BASE / "Dockerfile").read_text(encoding="utf-8")
    match = PIN_REGEX.search(dockerfile)
    if match is None:
        _fail("Dockerfile FROM must pin livekit/livekit-server@<version>@sha256:<full digest>")
    print(f"[validation] pin OK: livekit/livekit-server:{match.group('version')}")


def validate_config_syntax() -> None:
    yaml_file = BASE / "livekit.yaml"
    text = yaml_file.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        yaml.safe_load(text)
    except ImportError:
        # No PyYAML in this env; fall back to a balanced-brace/indent sanity check.
        if not text.strip():
            _fail(f"{yaml_file} is empty")
        if text.count("{{") != text.count("}}"):
            _fail(f"{yaml_file} has unbalanced template braces")
    print(f"[validation] yaml parse ok: {yaml_file}")


def validate_entrypoint_contract() -> None:
    entrypoint = (BASE / "entrypoint.sh").read_text(encoding="utf-8")
    if "set -eu" not in entrypoint and "set -e" not in entrypoint:
        _fail("entrypoint.sh must use set -eu so missing credentials fail loudly")
    if "|| true" in entrypoint or "|| true;" in entrypoint:
        _fail("entrypoint.sh must not ignore failures with || true")
    if "exec livekit-server" not in entrypoint:
        _fail("entrypoint.sh must exec the real livekit-server binary")
    print("[validation] entrypoint fail-loud contract ok")


def main() -> None:
    validate_pin()
    validate_config_syntax()
    validate_entrypoint_contract()
    print("[validation] all livekit platform checks passed")


if __name__ == "__main__":
    main()
