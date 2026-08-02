"""Task 1.2 structural and compatibility checks for canonical product services."""

from __future__ import annotations

import importlib
import os
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


def _isolated_root_import(code: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "APP_ENV": "dev",
        "DIRECTOR_ENABLED": "0",
        "LLM_ENGINE": "none",
        "RENDER_BACKEND": "mock",
        "SESSION_STORE": "memory",
        "TTS_ENGINE": "tone",
    }
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
        text=True,
    )


def test_isolated_root_imports_legacy_server_and_actual_canonical_app() -> None:
    result = _isolated_root_import(
        "import sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "from core.server import app as legacy_app; "
        "from services.product.backend_service.src.backend.main import app as canonical_app; "
        "assert canonical_app is legacy_app; "
        "print(canonical_app.title)"
    )

    assert result.stdout.strip()


def test_backend_compatibility_preserves_engine_ownership() -> None:
    result = _isolated_root_import(
        "import sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "from core.llm.base import ENGINES as llm_engines; "
        "from core.tts.base import ENGINES as tts_engines; "
        "assert {'openai_compat', 'remote'} <= llm_engines.keys(); "
        "assert {'remote_http', 'remote', 'elevenlabs', 'openai_speech'} "
        "<= tts_engines.keys(); "
        "assert all(llm_engines[name].__module__.startswith('llm.engines.') "
        "for name in ('llamacpp', 'sglang', 'hf', 'vllm')); "
        "assert all(tts_engines[name].__module__.startswith('tts.engines.') "
        "for name in ('transformers', 'vieneu', 'cosyvoice')); "
        "assert llm_engines['openai_compat'].__module__ == "
        "'core.llm.adapters.openai_compat'; "
        "assert tts_engines['remote_http'].__module__ == "
        "'core.tts.adapters.remote_http'"
    )

    assert result.returncode == 0


def test_service_dockerfiles_install_from_frozen_locks() -> None:
    for service in ("llm_service", "tts_service"):
        dockerfile = (_service_root(service) / "Dockerfile").read_text(encoding="utf-8")
        assert "uv sync --frozen" in dockerfile
        assert "uv pip install --system --no-cache-dir --no-deps /app" not in dockerfile


def test_canonical_backend_entrypoint_is_the_runtime_default() -> None:
    dockerfile = (_service_root("backend_service") / "Dockerfile").read_text(encoding="utf-8")
    start_script = (_service_root("backend_service") / "scripts" / "start.sh").read_text(
        encoding="utf-8"
    )

    assert 'CMD ["uvicorn", "backend.main:app"' in dockerfile
    assert "uvicorn backend.main:app" in start_script


def test_model_start_scripts_supply_default_commands() -> None:
    llm_start = (_service_root("llm_service") / "scripts" / "start.sh").read_text(
        encoding="utf-8"
    )
    tts_start = (_service_root("tts_service") / "scripts" / "start.sh").read_text(
        encoding="utf-8"
    )

    assert "if [[ $# -eq 0 ]]" in llm_start and "vllm serve" in llm_start
    assert "if [[ $# -eq 0 ]]" in tts_start and "vllm serve" in tts_start and "--omni" in tts_start


def test_legacy_imports_share_canonical_types() -> None:
    service_srcs = [str(_service_root(service) / "src") for service in SERVICES]
    sys.path[:0] = service_srcs
    importlib.invalidate_caches()

    from avatar.engines.base import RenderBackend as CanonicalRenderBackend
    from avatar.engines.mock import MockRenderBackend as CanonicalMockRenderBackend
    from avatar.engines.windows import TextChunk as CanonicalTextChunk
    from avatar.locks import SessionLockRegistry as CanonicalSessionLockRegistry
    from avatar.publishing import AudioTrackPublisher as CanonicalAudioTrackPublisher
    from avatar.queue import BoundedVideoQueue as CanonicalBoundedVideoQueue
    from core.livekit_publish import AudioTrackPublisher as LegacyAudioTrackPublisher
    from core.llm import LLMEngine as LegacyLlmEngine
    from core.render import RenderBackend as LegacyRenderBackend
    from core.render.locks import SessionLockRegistry as LegacySessionLockRegistry
    from core.render.mock import MockRenderBackend as LegacyMockRenderBackend
    from core.render.queue import BoundedVideoQueue as LegacyBoundedVideoQueue
    from core.render.windows import TextChunk as LegacyTextChunk
    from core.tts import TTSEngine as LegacyTtsEngine
    from llm import LLMEngine as CanonicalLlmEngine
    from tts import TTSEngine as CanonicalTtsEngine

    assert (
        LegacyLlmEngine,
        LegacyTtsEngine,
        LegacyRenderBackend,
        LegacyMockRenderBackend,
        LegacySessionLockRegistry,
        LegacyBoundedVideoQueue,
        LegacyTextChunk,
        LegacyAudioTrackPublisher,
    ) == (
        CanonicalLlmEngine,
        CanonicalTtsEngine,
        CanonicalRenderBackend,
        CanonicalMockRenderBackend,
        CanonicalSessionLockRegistry,
        CanonicalBoundedVideoQueue,
        CanonicalTextChunk,
        CanonicalAudioTrackPublisher,
    )
