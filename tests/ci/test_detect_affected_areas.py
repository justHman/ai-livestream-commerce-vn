"""Tests for OpenSpec 2.4 affected-area detection."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci.detect_affected_areas import (
    ALL_AREAS,
    BACKEND_SCHEMA_CONSUMERS,
    classify_path,
    detect_affected_areas,
)

ROOT = Path(__file__).resolve().parents[2]


# ── Direct owner ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("services/product/backend_service/src/backend/main.py", ["backend_service"]),
        ("services/product/llm_service/src/llm/config.py", ["llm_service"]),
        ("services/product/tts_service/src/tts/main.py", ["tts_service"]),
        ("services/product/avatar_service/src/avatar/main.py", ["avatar_service"]),
        ("services/platform/livekit/Dockerfile", ["platform_livekit"]),
        ("services/platform/lmcache/lmcache.yaml", ["platform_lmcache"]),
        ("services/platform/postgres/scripts/smoke_test.py", ["platform_postgres"]),
        ("services/platform/redis/redis.dev.conf", ["platform_redis"]),
        ("workbench/src/main.ts", ["workbench"]),
        ("frontend/stage2.html", ["workbench"]),
        ("infra/environments/dev/main.tf", ["infra"]),
        ("core/director/config.py", ["backend_service"]),
        ("providers/liveavatar_cloud/service/lite_agent.py", ["backend_service"]),
    ],
)
def test_direct_owner(path, expected):
    assert classify_path(path) == expected


# ── Service contract artifacts fan-out ──────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "services/product/tts_service/contracts/v1/openapi.json",
            ["backend_service", "tts_service"],
        ),
        (
            "services/product/llm_service/contracts/v1/openapi.json",
            ["backend_service", "llm_service"],
        ),
        (
            "services/product/avatar_service/contracts/v1/openapi.json",
            ["avatar_service", "backend_service"],
        ),
        (
            "services/product/backend_service/contracts/v1/openapi.json",
            ["backend_service", "workbench"],
        ),
        (
            "services/product/backend_service/contracts/v1/websocket/control.schema.json",
            ["backend_service", "workbench"],
        ),
    ],
)
def test_contract_fanout(path, expected):
    assert classify_path(path) == expected


def test_contract_never_recursive():
    areas = classify_path("services/product/tts_service/contracts/v1/openapi.json")
    assert "workbench" not in areas


# ── Canonical source DTOs fan-out (finding #7) ──────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "services/product/backend_service/src/backend/api/v1/schemas/sessions.py",
            ["backend_service", "workbench"],
        ),
        (
            "services/product/llm_service/src/llm/api/v1/schemas/chat.py",
            ["backend_service", "llm_service"],
        ),
        (
            "services/product/tts_service/src/tts/api/v1/schemas/speech.py",
            ["backend_service", "tts_service"],
        ),
        (
            "services/product/avatar_service/src/avatar/api/v1/schemas/sessions.py",
            ["avatar_service", "backend_service"],
        ),
    ],
)
def test_canonical_src_dto_fanout_all_four(path, expected):
    assert classify_path(path) == expected


def test_canonical_src_dto_not_recursive():
    areas = classify_path("services/product/llm_service/src/llm/api/v1/schemas/chat.py")
    assert "workbench" not in areas


def test_plain_service_src_not_dto():
    # A route/engine change does NOT fan out; only DTO schemas do.
    assert classify_path("services/product/llm_service/src/llm/engines/vllm.py") == ["llm_service"]


def test_backend_schema_fans_to_workbench():
    assert classify_path("core/api/v1/schemas/sessions.py") == sorted(BACKEND_SCHEMA_CONSUMERS)


# ── Shared config / locks / build (finding #8) ──────────────────────────────


def test_pyproject_toml_shared_config():
    assert classify_path("pyproject.toml") == ["shared-config"]


def test_uv_lock_shared_locks():
    assert classify_path("uv.lock") == ["shared-locks"]


def test_pyrightconfig_shared_config():
    assert classify_path("pyrightconfig.json") == ["shared-config"]


def test_ruff_editorconfig_shared_config():
    assert classify_path("ruff.toml") == ["shared-config"]
    assert classify_path(".editorconfig") == ["shared-config"]


def test_makefile_compose_shared_build():
    assert classify_path("Makefile") == ["shared-build"]
    assert classify_path("compose.yaml") == ["shared-build"]


def test_scripts_ci_is_ci_area():
    assert classify_path("scripts/ci/detect_affected_areas.py") == ["ci"]


def test_other_scripts_is_shared_source():
    assert classify_path("scripts/bench_api.py") == ["shared-source"]


def test_github_workflow_is_ci():
    assert classify_path(".github/workflows/ci.yml") == ["ci"]


# ── Docs neutral ────────────────────────────────────────────────────────────


def test_docs_neutral():
    assert classify_path("docs/architecture.md") == []
    assert classify_path("notes/foo.md") == []
    assert classify_path("README.md") == []
    assert classify_path("openspec/changes/x/proposal.md") == []


# ── Unknown conservative (no full fan-out, finding #8) ───────────────────


def test_unknown_path_conservative_shared_source():
    areas = classify_path("unknown/random/file.txt")
    assert areas == ["shared-source"]


def test_root_misc_file_conservative():
    assert classify_path("data.csv") == ["shared-source"]


# ── Finding 5: .dockerignore + root build-policy files ─────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        (".dockerignore", ["shared-build"]),
        ("Dockerfile", ["shared-build"]),
        ("Makefile", ["shared-build"]),
        ("compose.yaml", ["shared-build"]),
        ("docker-compose.yml", ["shared-build"]),
        ("docker-compose.yaml", ["shared-build"]),
    ],
)
def test_build_policy_files_mapped_to_shared_build(path, expected):
    assert classify_path(path) == expected


def test_dockerignore_shared_build():
    """.dockerignore controls build context for all services — shared-build."""
    assert classify_path(".dockerignore") == ["shared-build"]


def test_dockerignore_child_path_not_shared_build():
    """A .dockerignore inside a service dir is that service's concern."""
    assert classify_path("services/product/backend_service/.dockerignore") == ["backend_service"]


def test_multi_change_union():
    r = detect_affected_areas(
        [
            "services/product/tts_service/contracts/v1/openapi.json",
            "workbench/src/main.ts",
            "infra/environments/dev/main.tf",
        ]
    )
    assert r["areas"] == ["backend_service", "infra", "tts_service", "workbench"]


def test_dedup_union_deterministic():
    a = detect_affected_areas(["core/api/v1/schemas/sessions.py", "core/director/config.py"])
    b = detect_affected_areas(["core/director/config.py", "core/api/v1/schemas/sessions.py"])
    assert a["areas"] == b["areas"]
    assert a["matrix"] == b["matrix"]


def test_rename_safe_union():
    r = detect_affected_areas(
        [
            "services/product/tts_service/src/old_tts.py",
            "services/product/tts_service/src/tts/new_name.py",
        ]
    )
    assert r["areas"] == ["tts_service"]


# ── Matrix shape ────────────────────────────────────────────────────────────


def test_matrix_covers_all_areas():
    r = detect_affected_areas(["workbench/src/main.ts"])
    for area in ALL_AREAS:
        assert area in r["matrix"]
    assert r["matrix"]["workbench"] is True
    assert r["matrix"]["infra"] is False


def test_no_silent_unknown():
    # Unknown paths are classified (never dropped from by_path).
    r = detect_affected_areas(["weird/path"])
    assert r["unclassified"] == []
    assert r["areas"] == ["shared-source"]


# ── CLI smoke ───────────────────────────────────────────────────────────────


def test_cli_json_output():
    proc = subprocess.run(
        [
            "python",
            "scripts/ci/detect_affected_areas.py",
            "--json",
            "--paths",
            "services/product/llm_service/src/llm/main.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["areas"] == ["llm_service"]
