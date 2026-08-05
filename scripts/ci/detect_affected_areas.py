"""
Task 83 — OpenSpec 2.4: Repository-aware affected-area rules.

Deterministic tested detector taking changed paths and emitting booleans/matrices.

Areas:
- product services: backend_service, llm_service, tts_service, avatar_service
- platform runtimes: platform_livekit, platform_lmcache, platform_postgres, platform_redis
- workbench (frontend developer console)
- infra (Terraform)
- contracts (service-owned artifacts + consumer fan-out implied by service areas)
- shared-config, shared-locks, shared-build, shared-source (root shared files)
- ci (workflow/build definition)
- docs (neutral)

Rules:
- Direct owner paths select owner.
- Service contract artifacts select owner + exact consumers (one level):
    backend_service contract/source-DTO -> backend_service + workbench
    llm/tts/avatar contract/source-DTO   -> owner + backend_service
- Canonical source DTOs under
  `services/product/<service>/src/<pkg>/api/v1/schemas/` fan out like contracts
  (they are the shipped API surface).
- Root shared config / lock / build files map to explicit shared areas, never
  full fan-out. Every known root source-policy file is mapped (pyrightconfig.json,
  ruff.toml, .editorconfig, pyproject.toml, uv.lock, ...).
- Docs-only neutral unless runtime docs consumed by build.
- Unknown path -> conservative single shared-source area; a genuinely global
  unknown file is surfaced but never fans every service silently.

Renames/deletes handled (union is rename-safe; classification is path-based).
"""

import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set

# ── Areas ───────────────────────────────────────────────────────────────────

PRODUCT_SERVICES = ("backend", "llm", "tts", "avatar")
PLATFORM_RUNTIMES = ("livekit", "lmcache", "postgres", "redis")

PRODUCT_AREAS = frozenset(f"{s}_service" for s in PRODUCT_SERVICES)
PLATFORM_AREAS = frozenset(f"platform_{r}" for r in PLATFORM_RUNTIMES)

ALL_AREAS = (
    PRODUCT_AREAS
    | PLATFORM_AREAS
    | frozenset(
        {
            "workbench",
            "infra",
            "shared-config",
            "shared-locks",
            "shared-build",
            "shared-source",
            "ci",
            "docs",
        }
    )
)

# Service contract/source-DTO consumers (one level, never recursive).
#  backend_service -> backend + workbench (canonical /api/v1)
#  llm/tts/avatar  -> owner + backend (outbound client)
SERVICE_CONTRACT_CONSUMERS: Dict[str, frozenset] = {
    "backend_service": frozenset({"backend_service", "workbench"}),
    "llm_service": frozenset({"llm_service", "backend_service"}),
    "tts_service": frozenset({"tts_service", "backend_service"}),
    "avatar_service": frozenset({"avatar_service", "backend_service"}),
}

# Canonical source package name per product service (design §11 vocabulary).
SERVICE_SRC_PACKAGE = {
    "backend": "backend",
    "llm": "llm",
    "tts": "tts",
    "avatar": "avatar",
}

# Root shared files: explicit map from root filename to shared area. This is the
# complete list of known root source-policy / config / lock / build files; any
# unmatched root file falls to the shared-source conservative area.
ROOT_SHARED_AREA: Dict[str, str] = {
    # config
    "pyproject.toml": "shared-config",
    "ruff.toml": "shared-config",
    ".editorconfig": "shared-config",
    "pyrightconfig.json": "shared-config",
    "mypy.ini": "shared-config",
    ".python-version": "shared-config",
    # locks (dependency resolution -> runtime of all services)
    "uv.lock": "shared-locks",
    "poetry.lock": "shared-locks",
    "package-lock.json": "shared-locks",
    # build / tooling
    "Makefile": "shared-build",
    "Dockerfile": "shared-build",
    ".dockerignore": "shared-build",
    "compose.yaml": "shared-build",
    "docker-compose.yml": "shared-build",
    "docker-compose.yaml": "shared-build",
}

# Root paths that map to a specific area.
ROOT_PREFIX_AREA = {
    ".github/": "ci",
    "scripts/ci/": "ci",
    "scripts/": "shared-source",
    "infra/scripts/": "shared-build",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fanout(areas: Iterable[str]) -> List[str]:
    """Return sorted unique area list. Fan-out is baked into the rule keys."""
    return sorted(set(areas))


def _canonical_src_dto(p: str) -> List[str]:
    """Fan out canonical source DTOs be`<pkg>/api/v1/schemas/` roots.

    Matches `services/product/<service>_service/src/<pkg>/api/v1/schemas/`.
    These are the shipped API surface and consume like contract artifacts.
    """
    for svc in PRODUCT_SERVICES:
        pkg = SERVICE_SRC_PACKAGE[svc]
        prefix = f"services/product/{svc}_service/src/{pkg}/api/v1/schemas/"
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return _fanout(SERVICE_CONTRACT_CONSUMERS[f"{svc}_service"])
    return []


def classify_path(path: str) -> List[str]:
    """Classify a single changed path into owned area(s), with fan-out.

    Deterministic. Returns an empty list for docs/neutral paths.
    """
    p = path.replace("\\", "/").lstrip("/")

    # 1. Docs / neutral (not runtime-consumed)
    if p in (
        "README.md",
        "CLAUDE.md",
        "LICENSE",
        "CONTRIBUTING.md",
        ".gitignore",
    ):
        return []
    if p.startswith("docs/") or p.startswith("notes/") or p.startswith("openspec/"):
        return []

    # 2. Canonical source DTOs fan out like contracts (before direct-owner).
    dto = _canonical_src_dto(p)
    if dto:
        return dto

    # 3. Service contract artifacts (fan out to exact consumers, one level)
    for service in PRODUCT_SERVICES:
        contracts_dir = f"services/product/{service}_service/contracts"
        if p == contracts_dir or p.startswith(contracts_dir + "/"):
            return _fanout(SERVICE_CONTRACT_CONSUMERS[f"{service}_service"])

    # 5. Root shared files (explicit, required-area precision)
    if p in ROOT_SHARED_AREA:
        return [ROOT_SHARED_AREA[p]]

    # Match the MOST SPECIFIC prefix first (longest), so `scripts/ci/` wins
    # over `scripts/`.
    for prefix, area in sorted(ROOT_PREFIX_AREA.items(), key=lambda kv: len(kv[0]), reverse=True):
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return [area]

    # 6. Direct-owner paths
    for svc in PRODUCT_SERVICES:
        prefix = f"services/product/{svc}_service/"
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return [f"{svc}_service"]

    platform_prefix = {
        "livekit": "services/platform/livekit/",
        "lmcache": "services/platform/lmcache/",
        "postgres": "services/platform/postgres/",
        "redis": "services/platform/redis/",
    }
    for runtime, prefix in platform_prefix.items():
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return [f"platform_{runtime}"]

    if p == "workbench" or p.startswith("workbench/"):
        return ["workbench"]
    if p == "infra" or p.startswith("infra/"):
        return ["infra"]

    # 7. Unknown path -> conservative single shared-source area. It is surfaced
    # but never fans every service, and never silently dropped.
    return ["shared-source"]


def detect_affected_areas(changed_paths: List[str]) -> Dict[str, object]:
    """Compute the affected-area matrix for a set of changed paths.

    Returns:
        areas:          sorted list of distinct affected area ids
        matrix:         {area: bool} for every known area
        by_path:        {path: [areas]}
        unclassified:   paths that classified to no area (docs, neutral)
    """
    normalized = [p.replace("\\", "/").lstrip("/") for p in changed_paths]
    seen: Set[str] = set()
    unique = [p for p in normalized if not (p in seen or seen.add(p))]

    by_path: Dict[str, List[str]] = {}
    unclassified: List[str] = []

    for p in unique:
        areas = classify_path(p)
        by_path[p] = areas
        if not areas:
            unclassified.append(p)

    affected: Set[str] = set()
    for areas in by_path.values():
        affected |= set(areas)

    matrix = {area: (area in affected) for area in sorted(ALL_AREAS)}

    return {
        "areas": sorted(affected),
        "matrix": matrix,
        "by_path": by_path,
        "unclassified": unclassified,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI: classify changed paths from stdin (one per line) or --paths."""
    import argparse

    parser = argparse.ArgumentParser(description="Detect affected CI areas.")
    parser.add_argument("--paths", nargs="*", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if args.paths:
        changed = args.paths
    else:
        changed = [line for line in sys.stdin.read().splitlines() if line.strip()]

    if not changed:
        print("No paths provided.", file=sys.stderr)
        sys.exit(1)

    result = detect_affected_areas(changed)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Affected areas:", ", ".join(result["areas"]))
        for p, areas in result["by_path"].items():
            label = ", ".join(areas) if areas else "(neutral)"
            print(f"  {p}: {label}")
        if result["unclassified"]:
            print("Unclassified (neutral):", ", ".join(result["unclassified"]))


if __name__ == "__main__":
    main()
