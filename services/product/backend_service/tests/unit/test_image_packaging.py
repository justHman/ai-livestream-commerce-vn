"""Production-image packaging guard for runtime resources (audit R0.1).

SkillLoader / profanity / safety loaders resolve ``resources/`` relative to
the installed package (``Path(__file__).resolve().parents[6] / "resources"``),
so the runtime stage must COPY ``services/product/backend_service/resources/``
to the same tree as ``src/backend/``. A missing COPY only explodes at
container start — this guard fails the build definition instead.
"""

from __future__ import annotations

from pathlib import Path

_BACKEND_SERVICE_DIR = Path(__file__).resolve().parents[2]
_DOCKERFILE = _BACKEND_SERVICE_DIR / "Dockerfile"


def _runtime_stage() -> str:
    """Return the Dockerfile text of the final (runtime) stage."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    marker = "FROM python:"
    start = text.rfind(marker)
    assert start != -1, f"Dockerfile has no 'FROM python:' runtime stage: {_DOCKERFILE}"
    return text[start:]


def test_runtime_stage_copies_resources_directory() -> None:
    copy_lines = [
        line.strip()
        for line in _runtime_stage().splitlines()
        if line.strip().startswith("COPY") and "backend_service/resources/" in line
    ]
    assert len(copy_lines) == 1, (
        "expected exactly one runtime-stage COPY of backend_service/resources/, "
        f"found {len(copy_lines)}: {copy_lines}"
    )
    line = copy_lines[0]
    assert "--chown=appuser:appuser" in line, f"resources COPY missing --chown: {line}"
    assert "./services/product/backend_service/resources/" in line, (
        f"resources COPY has wrong destination (loaders expect "
        f"/app/services/product/backend_service/resources): {line}"
    )
