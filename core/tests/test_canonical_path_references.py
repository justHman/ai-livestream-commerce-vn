"""Audit canonical runtime/build paths during the staged monorepo migration."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_DOCKERFILES = {
    "backend_service": "backend",
    "llm_service": "llm",
    "tts_service": "tts",
    "avatar_service": "avatar",
}
PLATFORM_DOCKERFILES = ("livekit", "lmcache")
WORKFLOW_PATHS = (
    ".github/workflows/build-images.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy-dev.yml",
    ".github/workflows/deploy-prod.yml",
)
STALE_BUILD_PATHS = (
    "services/backend/Dockerfile",
    "services/llm/Dockerfile",
    "services/tts/Dockerfile",
    "services/avatar/Dockerfile",
    "services/${{ matrix.service }}/Dockerfile",
    "services/${{ matrix.svc }}/Dockerfile",
)


def test_canonical_dockerfiles_have_root_context_ignore_files() -> None:
    for service in PRODUCT_DOCKERFILES:
        root = ROOT / "services" / "product" / service
        assert (root / "Dockerfile").is_file()
        assert (root / "Dockerfile.dockerignore").is_file()

    for service in PLATFORM_DOCKERFILES:
        root = ROOT / "services" / "platform" / service
        assert (root / "Dockerfile").is_file()
        assert (root / "Dockerfile.dockerignore").is_file()


def test_workflows_reference_existing_canonical_dockerfiles() -> None:
    canonical_paths = {
        "services/product/backend_service/Dockerfile",
        "services/product/llm_service/Dockerfile",
        "services/product/tts_service/Dockerfile",
        "services/product/avatar_service/Dockerfile",
        "services/platform/livekit/Dockerfile",
        "services/platform/lmcache/Dockerfile",
    }
    workflow_text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in WORKFLOW_PATHS)

    assert all((ROOT / path).is_file() for path in canonical_paths)
    assert all(path in workflow_text for path in canonical_paths)
    assert not any(path in workflow_text for path in STALE_BUILD_PATHS)


def test_canonical_product_dockerfiles_only_copy_existing_paths() -> None:
    for service, package in PRODUCT_DOCKERFILES.items():
        dockerfile = (ROOT / "services" / "product" / service / "Dockerfile").read_text(
            encoding="utf-8"
        )

        assert f"services/product/{service}" in dockerfile
        assert f"src/{package}" in dockerfile


def test_root_metadata_declares_canonical_service_import_paths() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for service in PRODUCT_DOCKERFILES:
        assert f"services/product/{service}/src" in metadata
    assert 'include = ["core*", "providers*"]' not in metadata


def test_terraform_documents_canonical_image_owners() -> None:
    document = (ROOT / "infra/modules/compute/README.md").read_text(encoding="utf-8")

    for service in PRODUCT_DOCKERFILES:
        assert f"services/product/{service}/" in document
    for service in PLATFORM_DOCKERFILES:
        assert f"services/platform/{service}/" in document
