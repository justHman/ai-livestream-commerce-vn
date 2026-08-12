"""Stable Change A contract consumed by ``script_authoring`` (tasks 1.1/1.6/1.7).

Change A ``adaptive-speech-text-chunking`` (archived 2026-08-12) is the
downstream speech-segmentation dependency of this package. Its final
architecture is a cohesive ``backend/application/text_chunker/`` package:

    text_chunker/__init__.py  chunker.py  types.py  boundaries.py
    duration.py  policy.py  telemetry.py

Script authoring consumes ONLY the package-root exports listed below. It
MUST NOT deep-import an internal ``text_chunker`` module, must not import
the removed ``backend.application.speech_chunking`` namespace, and must not
import ``TextChunk`` from ``backend.application.render.windows``.

Readiness evidence lives in the archived Change A tasks file: strict
validation (task 9.3), the real-TTS VieNeu benchmark PASS (task 8.9), and
authorization of this change (task 9.5). ``verify_change_a_readiness``
fails when any of those markers or this package contract is absent.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

# Package-root exports Change B is allowed to consume from Change A.
EXPECTED_EXPORTS: tuple[str, ...] = (
    "TextChunker",
    "TextChunk",
    "SpeechDurationEstimator",
)

# Canonical Change A package layout (task 1.2).
EXPECTED_MODULES: tuple[str, ...] = (
    "chunker",
    "types",
    "boundaries",
    "duration",
    "policy",
)

# Evidence markers in the archived Change A tasks.md, in order.
_EVIDENCE_MARKERS: tuple[str, ...] = (
    "9.3 Run OpenSpec validation",  # strict validation task present
    "openspec validate adaptive-speech-text-chunking` → valid",
    "PASS 2026-08-12: real-TTS benchmark",  # task 8.9 VieNeu benchmark PASS
    "AUTHORIZED 2026-08-12 after 8.9 PASS",  # task 9.5 change B authorization
)

_ARCHIVE_TASKS = (
    Path(__file__).resolve().parents[7]
    / "openspec"
    / "changes"
    / "archive"
    / "2026-08-12-adaptive-speech-text-chunking"
    / "tasks.md"
)


def archived_change_a_tasks_path() -> Path:
    """Absolute path to the archived Change A tasks file (readiness evidence)."""
    return _ARCHIVE_TASKS


def verify_change_a_readiness() -> None:
    """Raise ``RuntimeError`` when any required Change A evidence is absent.

    Checks, in order: final package layout exists, the required exports are
    importable from the package root, the removed ``speech_chunking``
    namespace and sibling ``text_chunker.py`` facade are gone,
    ``render.windows`` does not define/re-export ``TextChunk``, adaptive
    policy config carries no fixed ``target_chars``, and the archived
    strict-validation / benchmark PASS / authorization markers exist.
    """
    import importlib.util

    from backend.application import text_chunker

    for module_name in EXPECTED_MODULES:
        if not hasattr(text_chunker, module_name):
            raise RuntimeError(
                f"Change A package module {module_name!r} missing from "
                f"backend.application.text_chunker"
            )
    for export in EXPECTED_EXPORTS:
        if not hasattr(text_chunker, export):
            raise RuntimeError(
                f"Change A package export {export!r} missing from "
                f"backend.application.text_chunker.__init__"
            )

    if importlib.util.find_spec("backend.application.speech_chunking") is not None:
        raise RuntimeError(
            "Removed namespace backend.application.speech_chunking still importable; "
            "Change A cleanup incomplete"
        )
    if importlib.util.find_spec("backend.application.text_chunker.chunker") is None:
        raise RuntimeError("text_chunker.chunker is not importable")

    from backend.application.render.windows import AudioWindow, VideoWindow  # noqa: F401

    import backend.application.render.windows as windows

    if hasattr(windows, "TextChunk"):
        raise RuntimeError(
            "render.windows must not define or re-export TextChunk; Change A cleanup "
            "incomplete"
        )

    from backend.application.text_chunker.policy import AdaptiveViPolicyConfig

    if hasattr(AdaptiveViPolicyConfig, "target_chars"):
        raise RuntimeError(
            "AdaptiveViPolicyConfig must not carry fixed-policy target_chars"
        )

    tasks = _ARCHIVE_TASKS
    if not tasks.is_file():
        raise RuntimeError(
            f"Change A readiness evidence missing: {tasks} (archive tasks.md)"
        )
    text = tasks.read_text(encoding="utf-8")
    missing = [marker for marker in _EVIDENCE_MARKERS if marker not in text]
    if missing:
        raise RuntimeError(
            "Change A readiness evidence incomplete; missing markers: "
            + "; ".join(missing)
        )


def iter_source_files(root: Path) -> Iterable[Path]:
    """Yield ``*.py`` files under ``root``, skipping caches (task 1.10 audit)."""
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


_FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"backend\.application\.speech_chunking"),
    re.compile(r"backend\.application\.render\.windows\s+import\s+.*TextChunk"),
    re.compile(r"from\s+backend\.application\.render\.windows\s+import\s+[^#\n]*TextChunk"),
    re.compile(r"from\s+backend\.application\.render\.windows\s+import\s+[^#\n]*\bTextChunk\b"),
)


def assert_no_legacy_chunker_imports(root: Path) -> None:
    """Fail when any source under ``root`` imports legacy chunker paths.

    Used by the task 1.10/15.8 architecture audit: zero active imports of
    ``speech_chunking`` or ``render.windows.TextChunk`` in script_authoring
    (and, at closeout, in the whole backend).
    """
    offenders: list[str] = []
    for path in iter_source_files(root):
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_IMPORT_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(root.parent.parent.parent)}")
                break
    if offenders:
        raise RuntimeError(
            "Legacy chunker imports found: " + ", ".join(sorted(set(offenders)))
        )
