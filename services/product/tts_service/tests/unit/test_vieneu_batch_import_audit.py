"""Import audit (task 7.11): the VieNeu batch engine stays provider-private.

``vieneu.v3_turbo_serve``/``V3TurboBatchEngine`` are internal SDK surfaces.
Only the provider adapter may reach them (via ``tts._get_batch_engine()``);
tests may fake them. Any other module referencing them fails the audit so a
leak into the scheduler/API layers is caught in CI, not at runtime.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PATTERN = ("v3_turbo_serve", "V3TurboBatchEngine")

# Adapter + its unit tests (+ this audit, which names the surface) are the
# only permitted touchpoints.
_ALLOWED = frozenset(
    {
        Path("src/tts/providers/vieneu_v3.py"),
        Path("tests/unit/test_vieneu_v3_provider.py"),
        Path("tests/unit/test_vieneu_batch_import_audit.py"),
    }
)


def _python_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def _offenders(directory: Path) -> list[str]:
    found = []
    for path in _python_files(directory):
        if path.relative_to(_ROOT) in _ALLOWED:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(marker in line for marker in _PATTERN):
                found.append(f"{path.relative_to(_ROOT)}:{line_no}")
    return found


def test_batch_engine_surface_confined_to_provider_boundary() -> None:
    offenders = _offenders(_ROOT / "src") + _offenders(_ROOT / "tests")
    assert offenders == [], (
        "VieNeu batch-engine surface referenced outside the provider adapter "
        "and its tests:\n" + "\n".join(offenders)
    )
