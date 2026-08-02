"""Task 1.2 structural and compatibility checks for canonical product services."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility for the shared test environment.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
SERVICES = {
    "backend_service": "backend",
    "llm_service": "llm",
    "tts_service": "tts",
    "avatar_service": "avatar",
}


def _service_root(service: str) -> Path:
    return ROOT / "services" / "product" / service


def _import_from_service(service: str, package: str) -> object:
    code = (
        "import importlib, pathlib, sys; "
        f"sys.path.insert(0, {str(_service_root(service) / 'src')!r}); "
        f"module = importlib.import_module({package!r}); "
        "print(pathlib.Path(module.__file__).resolve())"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize(("service", "package"), SERVICES.items())
def test_canonical_product_package_imports(service: str, package: str) -> None:
    imported = Path(_import_from_service(service, package))

    assert imported == (_service_root(service) / "src" / package / "__init__.py").resolve()


@pytest.mark.parametrize(("service", "package"), SERVICES.items())
def test_each_service_owns_install_metadata(service: str, package: str) -> None:
    root = _service_root(service)
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (root / "uv.lock").read_text(encoding="utf-8")

    assert metadata["tool"]["setuptools"]["package-dir"] == {"": "src"}
    assert f'name = "ai-livestream-{package}"' in lock


@pytest.mark.parametrize(("service", "package"), SERVICES.items())
def test_service_docker_and_start_script_use_canonical_package(
    service: str, package: str
) -> None:
    root = _service_root(service)
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    start_script = (root / "scripts" / "start.sh").read_text(encoding="utf-8")

    assert f"services/product/{service}" in dockerfile
    assert f"src/{package}" in dockerfile
    assert package in start_script


def test_legacy_imports_share_canonical_types() -> None:
    service_srcs = [str(_service_root(service) / "src") for service in SERVICES]
    sys.path[:0] = service_srcs
    importlib.invalidate_caches()

    from avatar.engines.base import RenderBackend as CanonicalRenderBackend
    from core.llm import LLMEngine as LegacyLlmEngine
    from core.render import RenderBackend as LegacyRenderBackend
    from core.tts import TTSEngine as LegacyTtsEngine
    from llm import LLMEngine as CanonicalLlmEngine
    from tts import TTSEngine as CanonicalTtsEngine

    assert (
        LegacyLlmEngine,
        LegacyTtsEngine,
        LegacyRenderBackend,
    ) == (
        CanonicalLlmEngine,
        CanonicalTtsEngine,
        CanonicalRenderBackend,
    )
