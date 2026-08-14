"""Drift-check committed v1 contract artifacts against the canonical apps.

Usage:
    python scripts/contracts/check.py [--scope backend|llm|tts|avatar]

Regenerates every artifact in memory and diffs it against the committed
file. Exits non-zero on ANY diff (CI drift gate). ``--scope`` restricts the
check to one service's artifacts plus the consumers of that artifact (see
the consumer map in ``services/product/backend_service/contracts/v1/README.md``);
an artifact change triggers only its owner's and known consumers' checks.
"""

from __future__ import annotations

import argparse
import sys

from generate import (
    CONTROL_SCHEMA,
    ROOT,
    SERVICES,
    dump_json,
    load_app,
    openapi_spec,
)

# Owner -> (artifact files relative to repo root). A scope regenerates and
# checks only the owner's artifacts plus consumers' artifacts when those
# consumers depend on the owner's contract (backend WS/HTTP consumed by
# workbench; backend llm/tts clients consume those services' contracts).
_ARTIFACTS = {
    "backend": (
        "services/product/backend_service/contracts/v1/openapi.json",
        "services/product/backend_service/contracts/v1/websocket/control.schema.json",
    ),
    "llm": ("services/product/llm_service/contracts/v1/openapi.json",),
    "tts": ("services/product/tts_service/contracts/v1/openapi.json",),
    "avatar": ("services/product/avatar_service/contracts/v1/openapi.json",),
}

# Scope -> artifacts checked. A changed owner contract fans out to its known
# consumers: backend OpenAPI/WS schemas -> workbench; llm/tts OpenAPI ->
# backend client contract tests.
_SCOPE_ARTIFACTS = {
    "backend": _ARTIFACTS["backend"],
    "llm": _ARTIFACTS["llm"] + _ARTIFACTS["backend"][:1],
    "tts": _ARTIFACTS["tts"] + _ARTIFACTS["backend"][:1],
    "avatar": _ARTIFACTS["avatar"],
}


def regenerate(service_name: str) -> dict[str, bytes]:
    """Return {relative_artifact_path: bytes} freshly generated for a service."""
    service = SERVICES[service_name]
    app = load_app(service)
    out: dict[str, bytes] = {}
    out[f"services/product/{service_name}_service/contracts/v1/openapi.json"] = dump_json(
        openapi_spec(app)
    )
    if service["ws_schemas"]:
        base = "services/product/backend_service/contracts/v1/websocket"
        out[f"{base}/control.schema.json"] = dump_json(CONTROL_SCHEMA)
    return out


def check(scope: str | None = None) -> int:
    """Regenerate in memory and diff against committed artifacts.

    Returns 0 when no diff, 1 when any committed artifact drifts.
    """
    targets = {
        path: (ROOT / path).read_bytes()
        for paths in _SCOPE_ARTIFACTS.values()
        if scope is None
        for path in paths
    }
    if scope is not None:
        targets = {path: (ROOT / path).read_bytes() for path in _SCOPE_ARTIFACTS[scope]}
    if not targets:
        print(f"error: no artifacts matched scope {scope!r}", file=sys.stderr)
        return 2

    regenerated = {
        path: data
        for name in SERVICES
        for path, data in regenerate(name).items()
        if path in targets
    }

    failures = []
    for path in sorted(targets):
        expected = targets[path]
        actual = regenerated[path]
        if expected != actual:
            failures.append(path)
    if failures:
        print("DRIFT DETECTED in committed contract artifacts:")
        for path in failures:
            print(f"  {path}")
        return 1
    print(f"OK: {len(targets)} artifact(s) match the canonical apps")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scope",
        choices=sorted(SERVICES),
        default=None,
        help="Check only this service's artifacts plus known consumers.",
    )
    args = parser.parse_args()
    return check(args.scope)


if __name__ == "__main__":
    raise SystemExit(main())
