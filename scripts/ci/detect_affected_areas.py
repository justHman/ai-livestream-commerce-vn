"""
Task 83 — OpenSpec 2.4: Repository-aware affected-area rules.

Deterministic tested detector taking changed paths and emitting booleans/matrices.

Areas:
- product services: backend_service, llm_service, tts_service, avatar_service
- platform runtimes: platform_livekit, platform_lmcache, platform_postgres, platform_redis
- workbench (frontend developer console, legacy path frontend/)
- infra (Terraform)
- contracts (service-owned artifacts + consumer fan-out)
- shared-config, shared-locks, shared-build (root shared files)
- ci (workflow/build definition)
- docs (neutral)

Rules:
- Direct owner paths select owner.
- Service contract selects owner + exact consumers:
    backend_service contract -> backend_service + workbench
    llm/tts/avatar contract   -> owner + backend_service (consumer)
- Backend shared schema (core/api/v1/schemas, core/schemas) -> backend + workbench.
- Shared source/build/CI/root tool changes fan only required areas,
  conservatively all where truly global.
- Renames/deletes handled (union is rename-safe; classification is path-based).
- Docs-only neutral unless runtime docs consumed by build.
- No silent unknown path; unknown -> safe conservative full fan-out.
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
            "contracts",
            "shared-config",
            "shared-locks",
            "shared-build",
            "ci",
            "docs",
        }
    )
)

# Service contract consumers.
#   backend_service contract is consumed by backend + workbench (canonical /api/v1).
#   llm/tts/avatar contracts are consumed by their owner + backend (outbound client).
SERVICE_CONTRACT_CONSUMERS: Dict[str, frozenset] = {
    "backend_service": frozenset({"backend_service", "workbench"}),
    "llm_service": frozenset({"llm_service", "backend_service"}),
    "tts_service": frozenset({"tts_service", "backend_service"}),
    "avatar_service": frozenset({"avatar_service", "backend_service"}),
}

# Shared schema roots fan to backend + workbench.
BACKEND_SCHEMA_CONSUMERS = frozenset({"backend_service", "workbench"})

# Contract artifact dirs (checked before direct-owner prefix match).
SERVICE_CONTRACT_DIRS = [f"services/product/{s}_service/contracts" for s in PRODUCT_SERVICES]


# ── Path prefix owners (ordered; first match wins) ─────────────────────────

_SERVICE_PREFIX = {s: f"services/product/{s}_service/" for s in PRODUCT_SERVICES}
_PLATFORM_PREFIX = {
    "livekit": "services/platform/livekit/",
    "lmcache": "services/platform/lmcache/",
    "postgres": "services/platform/postgres/",
    "redis": "services/platform/redis/",
}


def _fanout(areas: Iterable[str]) -> List[str]:
    """Return sorted area list. Fan-out is baked into classification rules,
    one level only (owner + exact consumers); never recursive.
    """
    return sorted(set(areas))


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

    # 2. Service contract artifacts (fan out to exact consumers, one level)
    for service in PRODUCT_SERVICES:
        contracts_dir = f"services/product/{service}_service/contracts"
        if p == contracts_dir or p.startswith(contracts_dir + "/"):
            return _fanout(SERVICE_CONTRACT_CONSUMERS[f"{service}_service"])

    # 3. Backend shared schema → backend + workbench
    if p.startswith("core/api/v1/schemas/") or p.startswith("core/schemas/"):
        return _fanout(BACKEND_SCHEMA_CONSUMERS)

    # 4. Root shared config / lock / build files
    shared_config = {
        "pyproject.toml": "shared-config",
        "ruff.toml": "shared-config",
        ".editorconfig": "shared-config",
        "uv.lock": "shared-locks",
        "Makefile": "shared-build",
        "README.md": None,
    }
    if p in shared_config:
        area = shared_config[p]
        return _fanout({area}) if area else []

    if p.startswith("scripts/ci/") or p.startswith("scripts/"):
        return ["ci"] if p.startswith("scripts/ci/") else _fanout({"shared-build"})

    if p.startswith(".github/"):
        return ["ci"]

    # 5. Direct-owner paths
    for svc in PRODUCT_SERVICES:
        prefix = _SERVICE_PREFIX[svc]
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return [f"{svc}_service"]

    for runtime, prefix in _PLATFORM_PREFIX.items():
        if p == prefix.rstrip("/") or p.startswith(prefix):
            return [f"platform_{runtime}"]

    if p == "workbench" or p.startswith("workbench/"):
        return ["workbench"]
    if p == "frontend" or p.startswith("frontend/"):
        return ["workbench"]  # legacy path is a workbench concern during Stage 2
    if p == "infra" or p.startswith("infra/"):
        return ["infra"]

    # 6. Legacy backend source roots
    if p.startswith("core/") or p.startswith("providers/"):
        return ["backend_service"]

    # 7. Unknown path → safe conservative result (all required areas)
    return _fanout(ALL_AREAS)


def detect_affected_areas(changed_paths: List[str]) -> Dict[str, object]:
    """Compute the affected-area matrix for a set of changed paths.

    Returns:
        areas:      sorted list of distinct affected area ids
        matrix:     {area: bool} for every known area
        by_path:    {path: [areas]}
        unclassified: paths that classified to no area (docs, neutral)
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
