"""Keep the service-ownership inventory tied to the pre-migration tree."""

from __future__ import annotations

import json
import re
from pathlib import Path


REQUIRED_CATEGORIES = {
    "product/backend",
    "product/llm",
    "product/tts",
    "product/avatar",
    "platform/livekit",
    "platform/lmcache",
    "platform/postgres",
    "platform/redis",
}
MAP_PATTERN = re.compile(
    r"<!-- service-ownership-map:start -->\s*```json\s*(?P<payload>.*?)\s*```\s*"
    r"<!-- service-ownership-map:end -->",
    re.DOTALL,
)


def test_service_ownership_map_covers_required_categories_and_existing_sources() -> None:
    repository_root = Path(__file__).parents[2]
    document = repository_root / "docs" / "service-ownership-map.md"
    match = MAP_PATTERN.search(document.read_text(encoding="utf-8"))
    assert match is not None, "service-ownership-map.md must contain the JSON ownership manifest"

    manifest = json.loads(match.group("payload"))
    mappings = manifest["mappings"]
    categories = {mapping["category"] for mapping in mappings}

    assert REQUIRED_CATEGORIES <= categories
    assert all(mapping["target"] for mapping in mappings)
    assert all((repository_root / mapping["source"]).exists() for mapping in mappings)
