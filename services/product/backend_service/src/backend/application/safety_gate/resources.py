"""Curated pattern-set resources for the safety gate (task 3.3).

Each safety check consumes a CURATED, VERSIONED pattern set shipped with the
backend (``resources/safety/curated_{kind}_v1.json``) — never a raw downloaded
dataset. Provenance and license metadata live in each resource itself; a
resource whose provenance is incomplete or not marked active is refused for
runtime use (task 3.6 activation guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "CuratedPatternSet",
    "load_all_curated_patterns",
    "load_curated_patterns",
]

# Default curated resources: service-level ``resources/safety/`` directory
# (mirrors the repository's resource convention). Resolved relative to the
# package so it works from a source checkout.
_RESOURCES_DIR = Path(__file__).resolve().parents[4] / "resources" / "safety"

_CURATED_KINDS = ("toxicity", "harassment", "unsafe_content")


class CuratedPatternSet:
    """Versioned curated phrase set with provenance.

    Patterns are lowercased on load; matching semantics belong to the
    consuming check (substring or token match), not to this resource.
    """

    def __init__(
        self,
        patterns: list[str],
        *,
        version: str,
        source: str,
        license: str,
        curated_by: str,
    ) -> None:
        self.patterns: frozenset[str] = frozenset(p.lower() for p in patterns)
        self.version = version
        self.source = source
        self.license = license
        self.curated_by = curated_by

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> CuratedPatternSet:
        """Build from the curated resource dict (validates provenance)."""
        provenance = resource.get("provenance", {})
        missing = [
            key for key in ("version", "source", "license", "curated_by") if not provenance.get(key)
        ]
        if missing:
            raise ValueError(f"curated pattern set provenance incomplete; missing {missing}")
        activation = provenance.get("activation_status")
        if activation != "active":
            raise ValueError(
                "curated pattern set is not activated for runtime use "
                f"(activation_status={activation!r}); complete provenance and "
                "false-positive review before activation"
            )
        return cls(
            resource.get("patterns", []),
            version=str(provenance["version"]),
            source=str(provenance["source"]),
            license=str(provenance["license"]),
            curated_by=str(provenance["curated_by"]),
        )


def load_curated_patterns(
    resource: Path | None = None,
    kind: str = "toxicity",
) -> CuratedPatternSet:
    """Load a curated pattern-set resource (default: packaged v1 of ``kind``)."""
    path = resource if resource is not None else _RESOURCES_DIR / f"curated_{kind}_v1.json"
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return CuratedPatternSet.from_resource(data)


def load_all_curated_patterns() -> dict[str, CuratedPatternSet]:
    """Load every curated safety pattern set, keyed by kind."""
    return {kind: load_curated_patterns(kind=kind) for kind in _CURATED_KINDS}
