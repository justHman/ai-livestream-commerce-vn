"""Container-build matrix derivation (P2-08).

GitHub's matrix ``include`` ALWAYS adds entries that do not match a vector
row as brand-new rows, so include-as-lookup cannot filter to affected
services. The correct pattern is a dedicated job that computes the exact
matrix for the affected services and hands it to ``container-build`` via
``strategy.matrix: ${{ fromJson(outputs.matrix) }}``.

This module owns the service -> build-config map so the CI job and the
repo-tool tests share one source of truth (no duplicated defaults).
"""

from __future__ import annotations

import argparse
import json
import sys

# Full service-area id (services_json value) -> container-build config.
# `scope` stays the short service name so the Buildx gha cache scope is
# stable per service (never carries the area id, branch, or SHA).
SERVICE_BUILD_CONFIG: dict[str, dict[str, str]] = {
    "backend_service": {
        "scope": "backend",
        "dockerfile": "services/product/backend_service/Dockerfile",
        "image": "imjusthman/ai-live-backend",
        "platforms": "linux/arm64",
    },
    "llm_service": {
        "scope": "llm",
        "dockerfile": "services/product/llm_service/Dockerfile",
        "image": "imjusthman/ai-live-llm",
        "platforms": "linux/amd64",
    },
    "tts_service": {
        "scope": "tts",
        "dockerfile": "services/product/tts_service/Dockerfile",
        "image": "imjusthman/ai-live-tts",
        "platforms": "linux/amd64",
    },
    "avatar_service": {
        "scope": "avatar",
        "dockerfile": "services/product/avatar_service/Dockerfile",
        "image": "imjusthman/ai-live-avatar",
        "platforms": "linux/amd64",
    },
}


def build_matrix(services: list[str]) -> list[dict[str, str]]:
    """Matrix rows for exactly the affected services (empty -> zero rows).

    Unknown service ids are skipped (never build an image for a service the
    matrix has no config for).
    """
    rows: list[dict[str, str]] = []
    for service in services:
        config = SERVICE_BUILD_CONFIG.get(service)
        if config is None:
            continue
        rows.append({"service": service, **config})
    return rows


def main() -> None:
    """CLI: ``build_matrix.py --services '["backend_service"]'`` -> JSON rows."""
    parser = argparse.ArgumentParser(description="Derive the container-build matrix.")
    parser.add_argument("--services", required=True, help="JSON array of affected service ids")
    args = parser.parse_args()
    try:
        services = json.loads(args.services)
    except json.JSONDecodeError as exc:
        print(f"invalid --services JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(build_matrix(services)))


if __name__ == "__main__":
    main()
