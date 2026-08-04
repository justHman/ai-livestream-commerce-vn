"""Audit canonical runtime/build paths during the staged monorepo migration."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_DOCKERFILES = {
    "backend_service": "backend",
    "llm_service": "llm",
    "tts_service": "tts",
    "avatar_service": "avatar",
}
PLATFORM_DOCKERFILES = ("livekit", "lmcache")
WORKFLOW_DOCKERFILES = {
    ".github/workflows/build-images.yml": {
        "services/product/backend_service/Dockerfile",
        "services/product/llm_service/Dockerfile",
        "services/product/tts_service/Dockerfile",
        "services/product/avatar_service/Dockerfile",
        "services/platform/livekit/Dockerfile",
        "services/platform/lmcache/Dockerfile",
    },
    ".github/workflows/ci.yml": {"services/product/backend_service/Dockerfile"},
    ".github/workflows/deploy-dev.yml": {
        "services/product/backend_service/Dockerfile",
        "services/product/llm_service/Dockerfile",
        "services/product/tts_service/Dockerfile",
        "services/product/avatar_service/Dockerfile",
        "services/platform/livekit/Dockerfile",
        "services/platform/lmcache/Dockerfile",
    },
    ".github/workflows/deploy-prod.yml": {
        "services/product/backend_service/Dockerfile",
        "services/product/llm_service/Dockerfile",
        "services/product/tts_service/Dockerfile",
        "services/product/avatar_service/Dockerfile",
        "services/platform/livekit/Dockerfile",
        "services/platform/lmcache/Dockerfile",
    },
}
STALE_BUILD_PATHS = (
    "services/backend/Dockerfile",
    "services/llm/Dockerfile",
    "services/tts/Dockerfile",
    "services/avatar/Dockerfile",
)


def _matrix_values(workflow: str, name: str) -> set[str]:
    return set(re.findall(rf"^\s+(?:-\s+)?{name}:\s*([^\s#]+)\s*$", workflow, re.MULTILINE))


def _workflow_dockerfiles(path: Path) -> set[str]:
    workflow = path.read_text(encoding="utf-8")
    lines = workflow.splitlines()
    pairs = [
        (line.split(":", 1)[1].strip(), lines[index + 1].split(":", 1)[1].strip())
        for index, line in enumerate(lines[:-1])
        if line.strip().startswith("context:") and lines[index + 1].strip().startswith("file:")
    ]
    assert pairs, f"{path} must declare Docker build context/file pairs"

    dockerfiles: set[str] = set()
    for context, dockerfile in pairs:
        assert context == ".", f"{path}: build context must remain repository root"
        if dockerfile == "${{ matrix.dockerfile }}":
            dockerfiles.update(_matrix_values(workflow, "dockerfile"))
        elif "${{ matrix.service }}" in dockerfile:
            dockerfiles.update(
                dockerfile.replace("${{ matrix.service }}", service)
                for service in _matrix_values(workflow, "service")
            )
        else:
            dockerfiles.add(dockerfile)

    assert all("${{" not in dockerfile for dockerfile in dockerfiles)
    return dockerfiles


def _local_copy_sources(dockerfile: Path) -> list[str]:
    sources: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY "):
            continue
        parts = shlex.split(line)
        if any(part.startswith("--from=") for part in parts[1:]):
            continue
        operands = [part for part in parts[1:] if not part.startswith("--")]
        assert len(operands) >= 2, f"{dockerfile}: invalid COPY instruction: {line}"
        sources.extend(operands[:-1])
    return sources


def _is_ignored(source: str, patterns: list[str]) -> bool:
    normalized = source.rstrip("/")
    ignored = False
    for pattern in patterns:
        include = pattern.startswith("!")
        pattern = pattern.lstrip("!").rstrip("/")
        matches = normalized == pattern or normalized.startswith(f"{pattern}/")
        matches = matches or any(fnmatch(part, pattern) for part in normalized.split("/"))
        if matches:
            ignored = not include
    return ignored


def test_workflows_individually_resolve_canonical_dockerfiles() -> None:
    for workflow_path, expected_dockerfiles in WORKFLOW_DOCKERFILES.items():
        resolved = _workflow_dockerfiles(ROOT / workflow_path)

        assert resolved == expected_dockerfiles
        assert all((ROOT / dockerfile).is_file() for dockerfile in resolved)
        assert not any(stale in resolved for stale in STALE_BUILD_PATHS)


def test_canonical_dockerfiles_copy_existing_nonignored_sources() -> None:
    dockerfiles = set().union(*WORKFLOW_DOCKERFILES.values())
    for dockerfile_path in dockerfiles:
        dockerfile = ROOT / dockerfile_path
        ignore_file = dockerfile.with_name("Dockerfile.dockerignore")
        assert ignore_file.is_file()
        patterns = [
            line.strip()
            for line in ignore_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        for source in _local_copy_sources(dockerfile):
            assert (ROOT / source.rstrip("/")).exists(), (
                f"{dockerfile}: missing COPY source {source}"
            )
            assert not _is_ignored(source, patterns), f"{dockerfile}: ignored COPY source {source}"


def test_canonical_product_dockerfiles_reference_service_packages() -> None:
    for service, package in PRODUCT_DOCKERFILES.items():
        dockerfile = (ROOT / "services" / "product" / service / "Dockerfile").read_text(
            encoding="utf-8"
        )

        assert f"services/product/{service}" in dockerfile
        assert f"src/{package}" in dockerfile


def test_product_dockerfiles_copy_build_metadata_before_locked_sync() -> None:
    for service in PRODUCT_DOCKERFILES:
        dockerfile = (ROOT / "services" / "product" / service / "Dockerfile").read_text(
            encoding="utf-8"
        )
        metadata_copy = next(
            line
            for line in dockerfile.splitlines()
            if line.startswith("COPY ") and f"services/product/{service}/pyproject.toml" in line
        )

        assert f"services/product/{service}/uv.lock" in metadata_copy
        assert f"services/product/{service}/README.md" in metadata_copy
        assert dockerfile.index(metadata_copy) < dockerfile.index("uv sync --frozen")


def test_product_locks_are_current() -> None:
    for service in PRODUCT_DOCKERFILES:
        result = subprocess.run(
            ["uv", "lock", "--check", "--project", str(ROOT / "services/product" / service)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr


def test_product_smoke_scripts_import_canonical_packages() -> None:
    for service, package in PRODUCT_DOCKERFILES.items():
        smoke_script = ROOT / "services" / "product" / service / "scripts" / "smoke_test.py"
        result = subprocess.run(
            [sys.executable, "-I", str(smoke_script)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        assert result.returncode == 0, result.stderr
        assert f"{package} " in result.stdout


def test_canonical_service_scripts_target_existing_launchers() -> None:
    for service in PRODUCT_DOCKERFILES:
        root = ROOT / "services" / "product" / service
        start_script = root / "scripts" / "start.sh"
        smoke_script = root / "scripts" / "smoke_test.py"

        assert start_script.is_file()
        assert smoke_script.is_file()
        if "../entrypoint.sh" in start_script.read_text(encoding="utf-8"):
            assert (root / "entrypoint.sh").is_file()


def test_documented_canonical_docker_commands_target_existing_files() -> None:
    documents = (
        ROOT / "services" / "README.md",
        *(ROOT / "services" / "product" / service / "README.md" for service in PRODUCT_DOCKERFILES),
    )
    for document in documents:
        dockerfiles = re.findall(r"docker build(?: [^\n]*)? -f ([^\s]+)", document.read_text())

        assert dockerfiles, f"{document} must document a canonical Docker build command"
        assert all((ROOT / dockerfile).is_file() for dockerfile in dockerfiles)


def test_root_metadata_declares_canonical_service_import_paths() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for service in PRODUCT_DOCKERFILES:
        assert f"services/product/{service}/src" in metadata
    assert 'include = ["core*", "providers*"]' not in metadata


def test_terraform_module_sources_resolve_from_each_environment() -> None:
    environments = tuple((ROOT / "infra" / "environments").glob("*/main.tf"))
    assert environments

    for config in environments:
        for source in re.findall(r'^\s*source\s*=\s*"([^"\n]+)"', config.read_text(), re.MULTILINE):
            assert (config.parent / source).resolve().is_dir(), f"{config}: missing module {source}"


def test_postgres_documentation_references_only_the_current_schema_owner() -> None:
    document = (ROOT / "services/platform/postgres/README.md").read_text(encoding="utf-8")

    assert "core/sql/runtime_schema.sql" in document
    assert "backend/db/sql/runtime_schema.sql" not in document
    assert (ROOT / "core/sql/runtime_schema.sql").is_file()


def test_notebook_bootstraps_before_provider_import_and_compiles() -> None:
    notebook = json.loads((ROOT / "notebooks/colab_demo.ipynb").read_text(encoding="utf-8"))
    code_cells = [
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"
    ]

    for source in code_cells:
        compile(source, "colab_demo.ipynb", "exec")
    bootstrap_cell = next(
        source for source in code_cells if "Provider module import passed" in source
    )
    assert bootstrap_cell.index("sys.path[:0]") < bootstrap_cell.index(
        "from providers.liveavatar_cloud.service import colab_server"
    )
    assert "services/product/backend_service/src" in bootstrap_cell


def _launcher_smoke_code(loader: str) -> str:
    return f"""
import os
import sys

os.environ.update(APP_ENV="dev", DIRECTOR_ENABLED="0", LLM_ENGINE="none", RENDER_BACKEND="mock", SESSION_STORE="memory", TTS_ENGINE="tone")
{loader}
assert sys.path[0] == str(module.BACKEND_SRC)
assert sys.path.count(str(module.BACKEND_SRC)) == 1
assert sys.path.count(str(module.REPO_ROOT)) == 1
from backend.main import app
assert app is not None
"""


def test_colab_launcher_bootstraps_imported_script_and_notebook_execution() -> None:
    launcher = ROOT / "providers/liveavatar_cloud/examples/colab_deploy.py"
    loaders = (
        f"""import importlib.util
spec = importlib.util.spec_from_file_location("colab_launcher", {str(launcher)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)""",
        f"""module = type("NotebookModule", (), {{"__dict__": {{}}}})()
namespace = {{"__name__": "colab_launcher", "__builtins__": __builtins__}}
exec(compile(open({str(launcher)!r}, encoding="utf-8").read(), "<colab-cell>", "exec"), namespace)
module = type("NotebookModule", (), namespace)""",
    )
    for loader in loaders:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _launcher_smoke_code(loader)],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

        assert result.returncode == 0


def test_colab_launcher_fails_loudly_when_no_repository_root_exists() -> None:
    launcher = ROOT / "providers/liveavatar_cloud/examples/colab_deploy.py"
    code = f"""
namespace = {{"__name__": "colab_launcher", "__builtins__": __builtins__}}
try:
    exec(compile(open({str(launcher)!r}, encoding="utf-8").read(), "<colab-cell>", "exec"), namespace)
except RuntimeError as error:
    assert "Tried:" in str(error)
else:
    raise AssertionError("expected missing repository root failure")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT.parent,
    )

    assert result.returncode == 0
