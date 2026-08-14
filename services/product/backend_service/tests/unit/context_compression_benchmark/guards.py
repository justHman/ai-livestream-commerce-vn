"""Task 18.7 guards: image context never carries control-plane material.

The hybrid design lets only read-only descriptive context become image
context (Decision 21). These guards prove the image-eligible section of the
benchmark corpus and the harness source contain none of:

- tool/response schema markers,
- instruction-hierarchy markers,
- authoritative volatile fact keys (price/stock/promotion/availability).

``assert_image_context_safe`` is the unit/static guard (same pattern as the
CI boundary guards in ``tests/ci``): it scans the real corpus file and the
harness source and raises ``RuntimeError`` on any violation. The negative
tests use SIMULATED corpus files (tmp_path) — never the real tree, mirroring
``test_envelope_boundary_guards``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 18.7 forbidden markers (see module docstring).
TOOL_SCHEMA_MARKERS = ("tool schema", "response schema", "json schema", "function calling")
INSTRUCTION_MARKERS = ("system instruction", "developer instruction", "instruction hierarchy")
VOLATILE_FACT_KEYS = ("price", "stock", "promotion", "availability")

_MARKERS = TOOL_SCHEMA_MARKERS + INSTRUCTION_MARKERS + VOLATILE_FACT_KEYS
_COMPILED = tuple(re.compile(re.escape(marker), re.IGNORECASE) for marker in _MARKERS)

# Source scanning is restricted to schema/instruction markers: English
# volatile-fact keys ("price", "availability", ...) are ordinary docstring
# vocabulary and cannot identify control-plane material in source code. The
# corpus scan (authoritative for volatile facts) uses the full marker set.
_SOURCE_MARKERS = TOOL_SCHEMA_MARKERS + INSTRUCTION_MARKERS
_SOURCE_COMPILED = tuple(re.compile(re.escape(marker), re.IGNORECASE) for marker in _SOURCE_MARKERS)


def forbidden_marker_in_source(line: str) -> str | None:
    """Return the first schema/instruction marker found in ``line``, or None."""
    for marker, pattern in zip(_SOURCE_MARKERS, _SOURCE_COMPILED):
        if pattern.search(line):
            return marker
    return None


def forbidden_marker(text: str) -> str | None:
    """Return the first forbidden marker found in ``text``, or None."""
    for marker, pattern in zip(_MARKERS, _COMPILED):
        if pattern.search(text):
            return marker
    return None


def _iter_descriptive_entries(corpus_path: Path):
    """Yield (label, text) for every image-eligible entry in the corpus.

    Only ``descriptive_context`` chunks become image context; fixture
    questions/answers are scoring ground truth and never enter the context,
    so they are intentionally not scanned (tool names like
    ``promotion_lookup`` are legitimate answer values).
    """
    data = json.loads(corpus_path.read_text(encoding="utf-8"))
    for chunk in data.get("descriptive_context", []):
        yield f"{corpus_path} :: descriptive_context[{chunk.get('id')}]", chunk.get("text", "")


def assert_image_context_safe(corpus_path: Path, source_path: Path | None = None) -> None:
    """Fail-loud when any image-eligible corpus entry (or the harness source,
    where the hybrid rendering is defined) carries a forbidden marker.

    Raises:
        RuntimeError: listing every label + marker violation.
    """
    violations: list[str] = []
    for label, text in _iter_descriptive_entries(corpus_path):
        marker = forbidden_marker(text)
        if marker is not None:
            violations.append(f"{label} contains forbidden marker {marker!r}")
    if source_path is not None:
        source = source_path.read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), start=1):
            if line.lstrip().startswith(("#", "//")):
                continue
            marker = forbidden_marker_in_source(line)
            if marker is not None:
                violations.append(f"{source_path}:{line_no} contains forbidden marker {marker!r}")
    if violations:
        raise RuntimeError(
            "image context must never carry control-plane material (18.7):\n"
            + "\n".join(violations)
        )
