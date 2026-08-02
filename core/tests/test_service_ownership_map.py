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
KNOWN_MOVED_SOURCES = {
    "services/livekit/",
    "services/lmcache/",
}
EXACT_TARGETS = {
    ".env.example": ".env.example",
    "core/llm/__init__.py": "services/product/llm_service/src/llm/__init__.py",
    "core/llm/adapters/__init__.py": "services/product/llm_service/src/llm/engines/__init__.py",
    "core/tts/__init__.py": "services/product/tts_service/src/tts/__init__.py",
    "core/tts/adapters/__init__.py": "services/product/tts_service/src/tts/engines/__init__.py",
    "providers/liveavatar_cloud/service/server.py": (
        "services/product/backend_service/src/backend/application/clients/avatar/liveavatar.py"
    ),
    "providers/liveavatar_cloud/service/conversation.py": "tests/sandbox/liveavatar/conversation.py",
    "providers/liveavatar_cloud/service/lite_agent.py": "tests/sandbox/liveavatar/lite_agent.py",
    "providers/liveavatar_cloud/service/store.py": "tests/sandbox/liveavatar/store.py",
}


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
    assert all(
        mapping["source"] in KNOWN_MOVED_SOURCES
        or (repository_root / mapping["source"]).exists()
        for mapping in mappings
    )
    targets = {mapping["source"]: mapping["target"] for mapping in mappings}
    assert {source: targets.get(source) for source in EXACT_TARGETS} == EXACT_TARGETS
