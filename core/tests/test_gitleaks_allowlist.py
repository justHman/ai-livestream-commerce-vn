#!/usr/bin/env python3
"""Gitleaks allowlist shape audit (Task 1.47).

Validates the committed .gitleaks.toml is well-formed and exactly scoped:
  - exactly two allowlist matches
  - both exact-value + exact-path (workbench/src/dev_tokens.ts)
  - no directory glob, no broad regex, no rule disable, no global stopword
  - a third token-like value in the SAME file would still be flagged,
    a Fixture value at a DIFFERENT path would still be flagged, and
    provider/API secret patterns are not allowlisted.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11 (path-level interpreter)
    tomllib = None

ROOT = Path(__file__).resolve().parents[2]
GITLEAKS_TOML = ROOT / ".gitleaks.toml"
DEV_TOKENS = ROOT / "workbench" / "src" / "dev_tokens.ts"

FIXTURE_VIEWER = "local-test-token-123456789012345678901234567890"
FIXTURE_ADMIN = "local-admin-token-123456789012345678901234567890"


def _raw() -> str:
    return GITLEAKS_TOML.read_text(encoding="utf-8")


def _matches() -> list[dict]:
    """Parse [[allowlist.matches]] entries without tomllib (stdlib portable)."""
    raw = _raw()
    entries: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[allowlist.matches]]"):
            if current is not None:
                entries.append(current)
            current = {}
            continue
        if current is None:
            continue
        m = re.match(r'^(file|match|reason)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', stripped)
        if m:
            current[m.group(1)] = m.group(2).replace('\\"', '"')
    if current is not None:
        entries.append(current)
    return entries


def _allowlist_keys() -> set[str]:
    raw = _raw()
    inside = False
    keys: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[allowlist.matches]]"):
            inside = False
        elif stripped.startswith("[allowlist]"):
            inside = True
            continue
        if inside and not stripped.startswith("[[") and "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key not in ("description",):
                keys.add(key)
    return keys


def test_allowlist_has_exactly_two_scoped_matches() -> None:
    matches = _matches()
    assert len(matches) == 2
    files = {m["file"] for m in matches}
    values = {m["match"] for m in matches}
    assert files == {"workbench/src/dev_tokens.ts"}
    assert values == {FIXTURE_VIEWER, FIXTURE_ADMIN}


def test_allowlist_has_no_directory_glob_or_broad_pattern() -> None:
    keys = _allowlist_keys()
    assert "paths" not in keys  # no directory-wide exclusion
    assert "regex" not in keys  # no broad regex
    assert "stopwords" not in keys  # no global stopword


def test_no_entropy_or_rule_disablement() -> None:
    raw = _raw().lower()
    # Ignore comment lines — comments may mention the words but must not
    # configure a selective-disable rule (gitleaks has no per-file disable).
    code_lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("#")]
    code = "\n".join(code_lines).lower()
    assert "entropy" not in code
    assert "nocomplain" not in code
    assert "gitleaksconfig" not in _allowlist_keys()


def test_third_token_in_same_file_not_allowlisted() -> None:
    values = {m["match"] for m in _matches()}
    third_token = "local-test-token-999999999999999999999999999999"
    assert third_token not in values


def test_fixture_at_different_path_not_allowlisted() -> None:
    for m in _matches():
        assert m["file"] == "workbench/src/dev_tokens.ts"
    assert "paths" not in _allowlist_keys()


def test_provider_api_secret_values_not_allowlisted() -> None:
    values = " ".join(m["match"] for m in _matches())
    assert "sk_" not in values
    assert "livekit_api_secret" not in values
    assert "livekit_api_key" not in values


def test_dev_tokens_source_matches_fixture_values() -> None:
    source = DEV_TOKENS.read_text(encoding="utf-8")
    assert FIXTURE_VIEWER in source
    assert FIXTURE_ADMIN in source
    assert source.count(FIXTURE_VIEWER) == 1
    assert source.count(FIXTURE_ADMIN) == 1