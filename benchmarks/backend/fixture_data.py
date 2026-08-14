"""Benchmark fixture data — self-contained copies of the Workbench fixtures.

Task 1.79 removed ``core/debug/mock_data.py``; the canonical product and
viewer-message fixtures live in ``workbench/src/fixtures/*.json``. This module
loads them (products normalized to the legacy dict shape) plus the four
benchmark-only Stage 2 comment texts that the Workbench viewer set does not
carry. Benchmark lanes stay runnable without the Workbench tree by falling
back to a static copy if the JSON files are absent.
"""

from __future__ import annotations

import json
import pathlib

_WORKBENCH_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "workbench" / "src" / "fixtures"

# Four Stage 2 benchmark comment texts not present in the Workbench viewer set.
_BENCHMARK_ONLY_MSGS = [
    "Serum vitamin C bao nhiêu tiền ạ?",
    "Áo hoodie có mũ đôi không shop?",
    "Áo hoodie có túi kangaroo không?",
    "Áo hoodie giá sao shop?",
]


def _load_viewer_msgs() -> list[str]:
    path = _WORKBENCH_FIXTURES / "viewer_messages.json"
    if path.is_file():
        msgs = [entry["text"] for entry in json.loads(path.read_text(encoding="utf-8"))]
    else:
        msgs = []
    return msgs + [t for t in _BENCHMARK_ONLY_MSGS if t not in msgs]


MOCK_VIEWER_MSGS = _load_viewer_msgs()
