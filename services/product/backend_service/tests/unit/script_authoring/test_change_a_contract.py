"""Change A contract boundary tests (C10 coverage boost).

Covers ``verify_change_a_readiness`` (the readiness gate), the source-audit
helpers (``iter_source_files`` / ``assert_no_legacy_chunker_imports``), and
the archived tasks path. The readiness gate was RED: the constant used
``PASS 2026-08-12: real-TTS benchmark`` (colon) while the archived tasks.md
reads ``PASS 2026-08-12. Real-TTS benchmark`` (period), so the gate always
raised. Fixed the constant to the period form.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from backend.application.script_authoring import change_a_contract


def test_archived_tasks_path_exists_and_is_file() -> None:
    tasks = change_a_contract.archived_change_a_tasks_path()
    assert tasks.exists()
    assert tasks.is_file()


def test_verify_change_a_readiness_passes() -> None:
    """The readiness gate must not raise when all Change A evidence is present."""
    change_a_contract.verify_change_a_readiness()


def test_iter_source_files_yields_py_and_skips_pycache(tmp_path: Path) -> None:
    root = tmp_path / "src"
    module = root / "backend" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("x = 1\n", encoding="utf-8")
    pycache = root / "backend" / "__pycache__"
    pycache.mkdir()
    (pycache / "cached.py").write_text("y = 2\n", encoding="utf-8")

    files = list(change_a_contract.iter_source_files(root))
    assert files == [module]


# ── readiness gate error guards (each branch must fail loudly) ──────────────


def test_verify_readiness_raises_missing_module(monkeypatch) -> None:
    monkeypatch.setattr(change_a_contract, "EXPECTED_MODULES", ("does_not_exist",))
    with pytest.raises(RuntimeError, match="missing"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_missing_export(monkeypatch) -> None:
    monkeypatch.setattr(change_a_contract, "EXPECTED_EXPORTS", ("DoesNotExist",))
    with pytest.raises(RuntimeError, match="export"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_speech_chunking_importable(monkeypatch) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "backend.application.speech_chunking" else None,
    )
    with pytest.raises(RuntimeError, match="speech_chunking"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_chunker_not_importable(monkeypatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="chunker"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_windows_reexports_textchunk(monkeypatch) -> None:
    import backend.application.render.windows as windows

    monkeypatch.setattr(windows, "TextChunk", type("TextChunk", (), {}), raising=False)
    with pytest.raises(RuntimeError, match="render.windows"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_policy_has_target_chars(monkeypatch) -> None:
    from backend.application.text_chunker.policy import AdaptiveViPolicyConfig

    monkeypatch.setattr(AdaptiveViPolicyConfig, "target_chars", 20, raising=False)
    with pytest.raises(RuntimeError, match="target_chars"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_archive_tasks_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(change_a_contract, "_ARCHIVE_TASKS", tmp_path / "missing" / "tasks.md")
    with pytest.raises(RuntimeError, match="tasks.md"):
        change_a_contract.verify_change_a_readiness()


def test_verify_readiness_raises_when_evidence_marker_missing(monkeypatch) -> None:
    monkeypatch.setattr(change_a_contract, "_EVIDENCE_MARKERS", ("NO-SUCH-MARKER-2026",))
    with pytest.raises(RuntimeError, match="missing markers"):
        change_a_contract.verify_change_a_readiness()


def test_assert_no_legacy_chunker_imports_passes_on_clean_root(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text("import os\nimport re\n", encoding="utf-8")
    change_a_contract.assert_no_legacy_chunker_imports(root)  # must not raise


@pytest.mark.parametrize(
    "bad_source",
    [
        "import backend.application.speech_chunking",
        "from backend.application.speech_chunking import chunk",
        "from backend.application.render.windows import TextChunk",
        "from backend.application.render.windows import TextChunk, AudioWindow",
        "from backend.application.render.windows import AudioWindow, TextChunk",
    ],
)
def test_assert_no_legacy_chunker_imports_raises_on_offenders(
    tmp_path: Path, bad_source: str
) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text(bad_source + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Legacy chunker imports found"):
        change_a_contract.assert_no_legacy_chunker_imports(root)
