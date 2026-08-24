"""Guard local configuration examples against stale engine/renderer names.

Canonical sources of truth:
- services/product/backend_service/src/backend/config.py (AppConfig accepted sets)
- openspec/specs/branch-governed-service-delivery/spec.md (ambiguous-selector rule)

Stale selectors ``openai_compat`` (as an engine), ``remote_http``, and
``remote_avatar`` were removed by branch-governed-service-delivery and must not
reappear in operator-facing examples.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXAMPLE_FILES = (
    ROOT / ".env.example",
    ROOT / "README.md",
    ROOT / "docs" / "architecture.md",
)

STALE_SELECTOR_PATTERN = re.compile(r"\b(openai_compat|remote_http|remote_avatar)\b")

CANONICAL_RENDER_BACKENDS = {
    "cloud_liveavatar",
    "self_host_avatarforcing_half",
    "self_host_echoavatar_full",
    "mock",
}


def test_no_stale_runtime_selectors_in_examples() -> None:
    offenders: list[str] = []
    for path in EXAMPLE_FILES:
        assert path.is_file(), f"missing example file: {path}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if STALE_SELECTOR_PATTERN.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "stale runtime selectors found:\n" + "\n".join(offenders)


def test_env_example_render_backend_value_is_canonical() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = [
        line.split("=", 1)[1].strip()
        for line in lines
        if line.startswith("RENDER_BACKEND=")
    ]
    assert values, ".env.example must define RENDER_BACKEND"
    assert values[0] in CANONICAL_RENDER_BACKENDS, (
        f"RENDER_BACKEND={values[0]!r} not in canonical set {sorted(CANONICAL_RENDER_BACKENDS)}"
    )
