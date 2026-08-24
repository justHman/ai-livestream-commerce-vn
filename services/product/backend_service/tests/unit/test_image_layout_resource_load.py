"""Filesystem-level simulation of the production image layout (audit R0.1).

``test_image_packaging.py`` pins the Dockerfile COPY that ships
``resources/``; this test proves the RUNTIME consequence. The Change-B
loaders resolve ``resources/`` relative to the installed package
(``Path(__file__).resolve().parents[6] / "resources"``), so a layout that
mirrors the built image WITHOUT the resources tree fails loudly, and the
same layout WITH the tree loads skill + profanity + safety resources and
reaches the generation prompt seam — all without LLM, network, GPU, or
Docker. The exact check code is the same ``_ARTIFACT_CHECK_SRC`` the
built-container smoke runs inside the image, so local proof == in-image
proof (which executes in CI).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_BACKEND_SERVICE_DIR = Path(__file__).resolve().parents[2]


def _load_check_src() -> str:
    """Import ``_ARTIFACT_CHECK_SRC`` from the built-container smoke script."""
    smoke = _BACKEND_SERVICE_DIR / "scripts" / "container_smoke.py"
    spec = importlib.util.spec_from_file_location("_c0_container_smoke", smoke)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module._ARTIFACT_CHECK_SRC  # type: ignore[attr-defined]


def _build_image_like_layout(root: Path, *, include_resources: bool) -> Path:
    """Mirror the runtime-stage layout: /app/services/product/backend_service.

    ``src/backend`` is always present (the image always has it); ``resources``
    is present only when the Dockerfile COPY is honored.
    """
    target = root / "services" / "product" / "backend_service"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    (target / "src").mkdir(parents=True)
    shutil.copytree(
        _BACKEND_SERVICE_DIR / "src" / "backend",
        target / "src" / "backend",
        ignore=ignore,
    )
    if include_resources:
        shutil.copytree(_BACKEND_SERVICE_DIR / "resources", target / "resources")
    return target


def _run_artifact_checks(target: Path, check_src: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target / "src")
    return subprocess.run(
        [sys.executable, "-c", check_src],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_image_layout_without_resources_fails_loudly() -> None:
    """Current-image layout (no resources) -> skill/profanity loads fail."""
    check_src = _load_check_src()
    with tempfile.TemporaryDirectory() as tmp:
        target = _build_image_like_layout(Path(tmp), include_resources=False)
        proc = _run_artifact_checks(target, check_src)
    assert proc.returncode != 0, "resource loads must fail when resources/ is absent"
    assert "not found" in proc.stdout + proc.stderr or "resources" in proc.stdout + proc.stderr


def test_image_layout_with_resources_loads_and_reaches_generation_seam() -> None:
    """Fixed-image layout -> skill/profanity/safety load and prompt seam works."""
    check_src = _load_check_src()
    with tempfile.TemporaryDirectory() as tmp:
        target = _build_image_like_layout(Path(tmp), include_resources=True)
        proc = _run_artifact_checks(target, check_src)
    assert proc.returncode == 0, f"artifact checks failed:\n{proc.stdout}\n{proc.stderr}"
    assert "ARTIFACT-CHECKS-OK" in proc.stdout


def test_content_hash_stable_across_layout() -> None:
    """SkillLoader.content_hash() in the layout == SHA-256 of packaged SKILL.md."""
    packaged = (
        _BACKEND_SERVICE_DIR / "resources" / "skills" / "livestream-sales-script" / "SKILL.md"
    )
    expected = hashlib.sha256(packaged.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        target = _build_image_like_layout(Path(tmp), include_resources=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(target / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from backend.application.script_authoring.generation.skill_loader import "
                "SkillLoader; print(SkillLoader().content_hash())",
            ],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == expected
